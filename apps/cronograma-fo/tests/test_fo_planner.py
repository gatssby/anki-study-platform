from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from app.db import initialize_or_migrate_database
from app.fo_planner import _pedagogical_order, build_fo_plan, select_eligible_fo_lessons
from app.reprogramming import ScheduleSettings, build_reprogram_report
from scripts.generate_adaptive_schedule import apply_assignments, build_report


def row(code: str, subject: str = "MAT", lesson_number: int = 1, **overrides):
    values = {
        "slot_key": f"S-{code}", "lesson_code": code, "track_code": "FO", "lesson_type": "lesson",
        "title_raw": f"{subject} Aula {lesson_number}", "portal_title": None, "duration_seconds": 1800,
        "subject_name": subject, "subject_prefix": subject, "module_label": "I", "module_number": 1,
        "lesson_number": lesson_number, "recommended_date": "2026-08-20", "slot_index": lesson_number,
        "is_seen": 0, "is_cut": 0, "cut_reason": None,
    }
    values.update(overrides)
    return values


class SharedFOPlannerUnitTest(unittest.TestCase):
    def eligible(self, rows):
        return select_eligible_fo_lessons(rows)[0]

    def test_starts_on_initial_date_with_or_without_un_backlog(self):
        lessons = self.eligible([row(f"MAT{i}", lesson_number=i) for i in range(1, 8)])
        first = build_fo_plan(lessons, start_date=date(2026, 7, 10), end_date=date(2026, 7, 16), include_weekends=True)
        second = build_fo_plan(lessons, start_date=date(2026, 7, 10), end_date=date(2026, 7, 16), include_weekends=True)
        self.assertEqual(first.assignments[0].day.date_value, date(2026, 7, 10))
        self.assertEqual(first.date_map, second.date_map)  # UN is not an input and cannot affect FO.

    def test_weekends_can_be_included_or_excluded_and_empty_original_days_are_used(self):
        lessons = self.eligible([row(f"BIO{i}", "BIO", i, recommended_date="2026-10-01", duration_seconds=120*60) for i in range(1, 8)])
        included = build_fo_plan(lessons, start_date=date(2026, 7, 10), end_date=date(2026, 7, 16), include_weekends=True)
        excluded = build_fo_plan(lessons, start_date=date(2026, 7, 10), end_date=date(2026, 7, 20), include_weekends=False)
        self.assertIn(date(2026, 7, 11), {a.day.date_value for a in included.assignments})
        self.assertIn(date(2026, 7, 12), {a.day.date_value for a in included.assignments})
        self.assertFalse(any(a.day.date_value.isoweekday() > 5 for a in excluded.assignments))
        self.assertTrue(all(a.day.date_value.isoformat() != "2026-10-01" for a in included.assignments))

    def test_one_hundred_lessons_are_all_distributed_across_ten_days(self):
        plan = build_fo_plan(
            self.eligible([row(f"W{i}", lesson_number=i) for i in range(100)]),
            start_date=date(2026,7,1), end_date=date(2026,7,10), include_weekends=True,
        )
        self.assertTrue(plan.is_feasible)
        self.assertEqual(len(plan.assignments), 100)
        self.assertEqual(len(plan.unallocated_lessons), 0)
        self.assertEqual([row["lesson_count"] for row in plan.daily_summary], [10] * 10)
        self.assertFalse(plan.daily_summary[0]["capacity_enforced"])

    def test_partial_and_total_unavailability(self):
        lessons = self.eligible([row(f"P{i}", lesson_number=i) for i in range(12)])
        plan = build_fo_plan(
            lessons, start_date=date(2026,7,10), end_date=date(2026,7,13), include_weekends=False,
            capacity_percent_by_date={date(2026,7,10): 50, date(2026,7,13): 100},
        )
        counts = [item["lesson_count"] for item in plan.daily_summary]
        self.assertLess(counts[0], counts[1])
        blocked = build_fo_plan(lessons, start_date=date(2026,7,10), end_date=date(2026,7,13), include_weekends=False,
            capacity_percent_by_date={date(2026,7,10): 0})
        self.assertIn(date(2026,7,10), blocked.skipped_dates)

    def test_one_thousand_lessons_fit_in_five_days_regardless_of_duration(self):
        exact = build_fo_plan(self.eligible([row("EX", duration_seconds=240*60)]), start_date=date(2026,7,10), end_date=date(2026,7,10), include_weekends=True)
        below = build_fo_plan(self.eligible([row("BE", duration_seconds=200*60)]), start_date=date(2026,7,10), end_date=date(2026,7,10), include_weekends=True)
        formerly_impossible = build_fo_plan(self.eligible([row(f"I{i}", lesson_number=i, duration_seconds=1000*60) for i in range(1000)]), start_date=date(2026,7,10), end_date=date(2026,7,14), include_weekends=True)
        self.assertTrue(exact.is_feasible and below.is_feasible)
        self.assertTrue(formerly_impossible.is_feasible)
        self.assertEqual(formerly_impossible.deficit_seconds, 0)
        self.assertEqual(len(formerly_impossible.assignments), 1000)
        self.assertEqual(len(formerly_impossible.unallocated_lessons), 0)
        self.assertEqual([row["lesson_count"] for row in formerly_impossible.daily_summary], [200] * 5)

    def test_individual_oversize_is_allocated_and_missing_duration_uses_45_minutes(self):
        plan = build_fo_plan(self.eligible([row("HUGE", duration_seconds=241*60)]), start_date=date(2026,7,10), end_date=date(2026,7,17), include_weekends=True)
        missing = build_fo_plan(self.eligible([row("MISSING", duration_seconds=0)]), start_date=date(2026,7,10), end_date=date(2026,7,10), include_weekends=True)
        self.assertTrue(plan.is_feasible)
        self.assertTrue(missing.is_feasible)
        self.assertEqual(plan.assignments[0].lesson.lesson_code, "HUGE")
        self.assertEqual(plan.unallocated_lessons, ())
        self.assertEqual(missing.assignments[0].lesson.lesson_code, "MISSING")
        self.assertEqual(missing.assignments[0].lesson.minutes, 45)
        self.assertEqual(missing.estimated_duration_lesson_codes, ("MISSING",))

    def test_no_assignment_exceeds_end_date_and_no_lesson_disappears(self):
        lessons = self.eligible([row(f"N{i}", lesson_number=i, duration_seconds=80*60) for i in range(10)])
        plan = build_fo_plan(lessons, start_date=date(2026,7,10), end_date=date(2026,7,12), include_weekends=True)
        self.assertTrue(all(item.day.date_value <= date(2026,7,12) for item in plan.assignments))
        self.assertEqual(len(plan.assignments) + len(plan.unallocated_lessons), len(lessons))
        self.assertTrue(all(item["lesson_count"] > 0 for item in plan.daily_summary))

    def test_seen_cut_english_and_review_free_are_excluded(self):
        rows = [
            row("OK"), row("SEEN", is_seen=1), row("CUT", is_cut=1, cut_reason="manual"),
            row("ENG", "LIN", is_cut=1, cut_reason="english"),
            row("REV", lesson_type="review", title_raw="Revisão / Livre"),
            row("FREE", title_raw="Dia livre"),
        ]
        lessons, _, _ = select_eligible_fo_lessons(rows)
        self.assertEqual([item.lesson_code for item in lessons], ["OK"])

    def test_result_is_deterministic_and_not_driven_by_problematic_dates(self):
        base = [row(f"{s}{i}", s, i) for s in ("BIO", "MAT", "HIS", "POR") for i in range(1, 4)]
        shifted = [{**item, "recommended_date": "2026-10-20"} for item in base]
        kwargs = dict(start_date=date(2026, 7, 10), end_date=date(2026, 7, 15), include_weekends=True)
        self.assertEqual(build_fo_plan(self.eligible(base), **kwargs).date_map,
                         build_fo_plan(self.eligible(shifted), **kwargs).date_map)

    def test_different_durations_never_reorder_lessons_from_the_same_subject(self):
        lessons = self.eligible([
            row(f"POR2A{i}", "POR", i, module_number=2, duration_seconds=i * 60)
            for i in range(1, 11)
        ])
        plan = build_fo_plan(
            lessons,
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 11),
            include_weekends=True,
        )

        self.assertEqual(
            [assignment.lesson.lesson_number for assignment in plan.assignments],
            list(range(1, 11)),
        )

    def test_assignments_preserve_the_exact_pedagogical_order_across_subjects(self):
        start_date = date(2026, 7, 10)
        lessons = self.eligible([
            row(f"{subject}{number}", subject, number)
            for subject in ("BIO", "MAT", "HIS", "POR")
            for number in range(1, 3)
        ])
        pedagogical = _pedagogical_order(lessons, start_date)
        lessons = [
            replace(lesson, duration_seconds=(index + 1) * 60)
            for index, lesson in enumerate(pedagogical)
        ]
        pedagogical = _pedagogical_order(lessons, start_date)

        plan = build_fo_plan(
            lessons,
            start_date=start_date,
            end_date=date(2026, 7, 11),
            include_weekends=True,
        )

        self.assertEqual(
            [assignment.lesson.lesson_code for assignment in plan.assignments],
            [lesson.lesson_code for lesson in pedagogical],
        )


class SharedFOPlannerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fo-planner-test-")
        self.db_path = Path(self.tmp.name) / "cronograma.db"
        initialize_or_migrate_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path); self.conn.row_factory = sqlite3.Row
        self.conn.execute("UPDATE schedule_settings SET target_finish_date='2026-07-16', exam_date='2026-08-01', include_weekends=1")
        for i, subject in enumerate(("BIO", "MAT", "HIS", "POR", "FIS", "QUI", "GEO"), 1):
            self.insert(row(f"{subject}{i}", subject, i))
        for i in range(1, 41):
            self.insert(row(f"UN{i}", "UN", i, track_code="UN", title_raw=f"UN Aula {i}", subject_name="UN", subject_prefix="UN"))
        self.insert(row("SEEN", is_seen=1)); self.insert(row("CUT", is_cut=1, cut_reason="manual"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def insert(self, item):
        self.conn.execute("""INSERT INTO lessons (slot_key,lesson_code,track_code,lesson_type,title_raw,portal_title,
            duration_seconds,subject_name,subject_prefix,module_label,module_number,lesson_number,week_number,
            day_index,day_name,slot_index,recommended_date,is_seen,is_cut,cut_reason,source_sheet)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["slot_key"],item["lesson_code"],item["track_code"],item["lesson_type"],item["title_raw"],
             item["portal_title"],item["duration_seconds"],item["subject_name"],item["subject_prefix"],
             item["module_label"],item["module_number"],item["lesson_number"],1,1,"Segunda",item["slot_index"],
             item["recommended_date"],item["is_seen"],item["is_cut"],item["cut_reason"],"test"))

    def dump(self):
        return "\n".join(self.conn.iterdump())

    def reports(self):
        settings = ScheduleSettings(exam_date=date(2026,8,1), target_finish_date=date(2026,7,16), include_weekends=True)
        web = build_reprogram_report(self.conn, settings, as_of_date=date(2026,7,10))
        web_map = {a.lesson.lesson_code: d.date_value.isoformat() for d in web.available_days for a in d.assignments if a.lesson.track_code == "FO"}
        args = argparse.Namespace(target_end_date="2026-07-16", study_weekends=True, cut_subject=[],
            daily_capacity_minutes=None, preserve_past=None, adaptive_mode=True, as_of_date="2026-07-10", apply=False)
        script_report, assignments = build_report(self.conn, args, self.db_path)
        return web, script_report, assignments

    def plans(self):
        web, _, assignments = self.reports()
        web_map = {a.lesson.lesson_code: d.date_value.isoformat() for d in web.available_days for a in d.assignments if a.lesson.track_code == "FO"}
        return web_map, {a.lesson.lesson_code: a.day.date_value.isoformat() for a in assignments}, assignments

    def test_web_and_sync_preserve_identical_fo_order_with_large_un_backlog(self):
        web, _, assignments = self.reports()
        web_codes = [
            assignment.lesson.lesson_code
            for day in web.available_days
            for assignment in day.assignments
            if assignment.lesson.track_code == "FO"
        ]
        sync_codes = [assignment.lesson.lesson_code for assignment in assignments]
        self.assertEqual(web_codes, sync_codes)
        self.assertEqual(web.available_days[0].date_value.isoformat(), "2026-07-10")

    def test_fo_and_un_are_all_distributed_without_a_shared_cap(self):
        web, _, _ = self.reports()
        self.assertTrue(web.feasible)
        self.assertEqual(web.assignment_count, web.pending_lesson_count)
        self.assertEqual(web.unallocated_lesson_count_by_track, {})

    def test_both_dry_runs_are_read_only_and_deterministic(self):
        before = self.dump(); first = self.plans(); second = self.plans()
        self.assertEqual(first[0], second[0]); self.assertEqual(first[1], second[1])
        self.assertEqual(before, self.dump())

    def test_web_and_sync_report_the_same_fo_feasibility_and_capacity(self):
        web, script, _ = self.reports()
        self.assertEqual(web.fo_plan_summary["standalone_is_feasible"], script["is_feasible"])
        self.assertEqual(web.fo_plan_summary["total_load_seconds"], script["total_load_seconds"])
        self.assertEqual(web.fo_plan_summary["total_capacity_seconds"], script["total_capacity_seconds"])
        self.assertEqual(web.fo_plan_summary["deficit_seconds"], script["deficit_seconds"])

    def test_applying_fo_assignments_in_temp_database_preserves_all_un_columns(self):
        _, _, assignments = self.plans()
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM lessons WHERE track_code='UN' ORDER BY lesson_code")]
        apply_assignments(self.conn, assignments, date(2026,7,10)); self.conn.commit()
        after = [tuple(r) for r in self.conn.execute("SELECT * FROM lessons WHERE track_code='UN' ORDER BY lesson_code")]
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT recommended_date FROM lessons WHERE lesson_code='SEEN'").fetchone()[0], "2026-08-20")
        self.assertEqual(self.conn.execute("SELECT recommended_date FROM lessons WHERE lesson_code='CUT'").fetchone()[0], "2026-08-20")
