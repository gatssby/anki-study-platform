from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from app.dashboard import fetch_database_rows
from app.db import initialize_or_migrate_database
from app.fo_planner import _pedagogical_order, select_eligible_fo_lessons
from app.reprogramming import (
    LessonCandidate,
    PlannedAssignment,
    PlannedDay,
    ScheduleSettings,
    apply_reprogramming,
    build_lesson_order_diagnostics,
    build_reprogram_report,
    fetch_schedulable_rows,
    planned_assignment_positions,
)
from scripts.reprogram_schedule import print_lesson_order_diagnostics


SCRAMBLED_POR2_DATES = {
    1: "2026-09-03",
    2: "2026-09-04",
    3: "2026-09-07",
    4: "2026-09-05",
    5: "2026-09-05",
    6: "2026-09-06",
    7: "2026-09-07",
    8: "2026-09-05",
    9: "2026-09-06",
    10: "2026-09-06",
    11: "2026-09-07",
    12: "2026-09-08",
    13: "2026-09-09",
    14: "2026-09-09",
    15: "2026-09-10",
    16: "2026-09-11",
    17: "2026-09-11",
    18: "2026-09-12",
    19: "2026-09-13",
    20: "2026-09-13",
    21: "2026-09-14",
    22: "2026-09-15",
    23: "2026-09-15",
}


def schedule_settings(target: date) -> ScheduleSettings:
    return ScheduleSettings(
        target_finish_date=target,
        include_weekends=True,
        max_daily_minutes_weekday=1,
        max_daily_minutes_saturday=1,
        max_daily_minutes_sunday=1,
    )


class PedagogicalReprogrammingIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="pedagogical-reprogram-")
        self.db_path = Path(self.tempdir.name) / "cronograma.db"
        initialize_or_migrate_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def insert_fo_lesson(
        self,
        code: str,
        *,
        subject: str,
        module_number: int,
        lesson_number: int,
        recommended_date: str,
        duration_minutes: int,
        slot_index: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw,
                duration_seconds, subject_name, subject_prefix, module_label,
                module_number, lesson_number, week_number, day_index, day_name,
                slot_index, recommended_date, is_seen, is_cut, source_sheet
            ) VALUES (?, ?, 'FO', 'lesson', ?, ?, ?, ?, ?, ?, ?, 1, 1,
                      'Segunda', ?, ?, 0, 0, 'test')
            """,
            (
                f"S-{code}",
                code,
                code,
                duration_minutes * 60,
                subject,
                subject,
                str(module_number),
                module_number,
                lesson_number,
                slot_index if slot_index is not None else lesson_number,
                recommended_date,
            ),
        )

    def insert_por2(self, *, count: int = 23, varied_durations: bool = False) -> None:
        for number in range(1, count + 1):
            duration = (1 if number % 2 else 600) if varied_durations else 90
            self.insert_fo_lesson(
                f"POR2A{number}",
                subject="POR",
                module_number=2,
                lesson_number=number,
                recommended_date=SCRAMBLED_POR2_DATES.get(number, "2026-09-15"),
                duration_minutes=duration,
                slot_index=count - number + 1,
            )

    @staticmethod
    def assignment_codes(report: object) -> list[str]:
        return [
            assignment.lesson.lesson_code
            for day in report.available_days
            for assignment in day.assignments
            if assignment.lesson.track_code == "FO"
        ]

    def test_complete_por2_apply_replaces_scrambled_dates_and_old_assignments(self) -> None:
        self.insert_por2()
        self.conn.execute(
            """
            INSERT INTO daily_assignments
                (dashboard_date, planned_slot_key, assigned_lesson_code)
            VALUES ('2026-09-05', 'old-slot', 'POR2A8')
            """
        )
        self.conn.execute(
            """
            INSERT INTO un_daily_assignments
                (dashboard_date, row_index, assigned_lesson_code)
            VALUES ('2026-09-05', 1, 'POR2A4')
            """
        )
        self.conn.commit()

        applied = apply_reprogramming(
            self.conn,
            schedule_settings(date(2026, 9, 9)),
            as_of_date=date(2026, 9, 3),
            db_path=self.db_path,
            diagnostic_lesson_prefixes=("POR2",),
        )
        self.conn.commit()

        expected_codes = [f"POR2A{number}" for number in range(1, 24)]
        self.assertEqual(self.assignment_codes(applied), expected_codes)
        expected_positions = planned_assignment_positions(applied.available_days)
        persisted_rows = self.conn.execute(
            """
            SELECT lesson_code, recommended_date, slot_index
            FROM lessons
            WHERE lesson_code LIKE 'POR2A%'
            ORDER BY lesson_number
            """
        ).fetchall()
        self.assertEqual(len(persisted_rows), 23)
        persisted_positions = [
            (row["recommended_date"], row["slot_index"]) for row in persisted_rows
        ]
        self.assertEqual(persisted_positions, sorted(persisted_positions))
        self.assertEqual(
            {
                row["lesson_code"]: (row["recommended_date"], row["slot_index"])
                for row in persisted_rows
            },
            {code: expected_positions[code] for code in expected_codes},
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_assignments").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM un_daily_assignments").fetchone()[0],
            0,
        )

        base_rows = fetch_database_rows(
            self.conn,
            status_filter="unseen",
            track_filter="FO",
            subject_filter="POR",
            front_filter="2",
            search="",
            limit=100,
            offset=0,
        )
        self.assertEqual([row["lesson_code"] for row in base_rows], expected_codes)
        self.assertEqual(
            [(row["recommended_date"], row["slot_index"]) for row in base_rows],
            persisted_positions,
        )
        diagnostic = applied.lesson_order_diagnostics[0]
        self.assertEqual(diagnostic["pedagogical_monotonicity"], "ok")
        output = io.StringIO()
        with redirect_stdout(output):
            print_lesson_order_diagnostics(applied.lesson_order_diagnostics)
        self.assertIn("pedagogical_monotonicity=ok", output.getvalue())

    def test_extreme_durations_cannot_reorder_a_single_pedagogical_sequence(self) -> None:
        self.insert_por2(count=10, varied_durations=True)
        self.conn.commit()
        report = build_reprogram_report(
            self.conn,
            schedule_settings(date(2026, 9, 7)),
            as_of_date=date(2026, 9, 3),
        )
        self.assertEqual(
            self.assignment_codes(report),
            [f"POR2A{number}" for number in range(1, 11)],
        )

    def test_multiple_subjects_keep_the_exact_pedagogical_interleaving(self) -> None:
        for number in range(1, 7):
            self.insert_fo_lesson(
                f"MAT1A{number}",
                subject="MAT",
                module_number=1,
                lesson_number=number,
                recommended_date=f"2026-09-{number + 2:02d}",
                duration_minutes=600 if number == 2 else 1,
            )
            self.insert_fo_lesson(
                f"POR2A{number}",
                subject="POR",
                module_number=2,
                lesson_number=number,
                recommended_date=f"2026-09-{number + 3:02d}",
                duration_minutes=600 if number == 3 else 1,
            )
        self.conn.commit()
        rows = fetch_schedulable_rows(self.conn)
        lessons, _, _ = select_eligible_fo_lessons(rows)
        expected = [
            lesson.lesson_code
            for lesson in _pedagogical_order(lessons, date(2026, 9, 3))
        ]
        report = build_reprogram_report(
            self.conn,
            schedule_settings(date(2026, 9, 7)),
            as_of_date=date(2026, 9, 3),
        )
        self.assertEqual(self.assignment_codes(report), expected)

    def test_one_thousand_lessons_fit_five_days_without_capacity_or_order_loss(self) -> None:
        for number in range(1, 1001):
            self.insert_fo_lesson(
                f"POR2A{number}",
                subject="POR",
                module_number=2,
                lesson_number=number,
                recommended_date="2026-09-03",
                duration_minutes=10_000,
            )
        self.conn.commit()
        report = build_reprogram_report(
            self.conn,
            schedule_settings(date(2026, 9, 7)),
            as_of_date=date(2026, 9, 3),
        )
        self.assertTrue(report.feasible)
        self.assertEqual(report.assignment_count, 1000)
        self.assertEqual(
            self.assignment_codes(report),
            [f"POR2A{number}" for number in range(1, 1001)],
        )
        positions = [
            planned_assignment_positions(report.available_days)[f"POR2A{number}"]
            for number in range(1, 1001)
        ]
        self.assertEqual(positions, sorted(positions))


class PedagogicalDiagnosticTest(unittest.TestCase):
    def test_diagnostic_prints_first_date_and_slot_violation(self) -> None:
        def candidate(code: str, number: int) -> LessonCandidate:
            return LessonCandidate(
                lesson_code=code,
                track_code="FO",
                subject_prefix="POR",
                group_label="POR / 2",
                weight_units=1,
                original_order=number,
                current_date=date(2026, 9, 3),
                row={},
            )

        rows = [
            {
                "lesson_code": f"POR2A{number}",
                "module_number": 2,
                "lesson_number": number,
                "slot_index": number,
                "recommended_date": "2026-09-03",
                "is_seen": 0,
                "is_cut": 0,
                "lesson_type": "lesson",
                "track_code": "FO",
                "title_raw": f"Aula {number}",
                "subject_name": "POR",
                "subject_prefix": "POR",
            }
            for number in range(1, 5)
        ]
        ordered = [candidate(f"POR2A{number}", number) for number in range(1, 5)]
        day_early = PlannedDay(date(2026, 9, 5), True, 1, 100)
        day_late = PlannedDay(date(2026, 9, 7), True, 1, 100)
        day_early.assignments.extend(
            [
                PlannedAssignment(ordered[0], day_early.date_value),
                PlannedAssignment(ordered[3], day_early.date_value),
            ]
        )
        day_late.assignments.extend(
            [
                PlannedAssignment(ordered[1], day_late.date_value),
                PlannedAssignment(ordered[2], day_late.date_value),
            ]
        )
        diagnostics = build_lesson_order_diagnostics(
            rows=rows,
            ordered_fo_lessons=ordered,
            planned_days=[day_early, day_late],
            unallocated_candidates=[],
            prefixes=("POR2",),
        )
        self.assertEqual(diagnostics[0]["pedagogical_monotonicity"], "fail")
        self.assertEqual(diagnostics[0]["first_violation"]["previous_lesson_code"], "POR2A3")
        self.assertEqual(diagnostics[0]["first_violation"]["lesson_code"], "POR2A4")

        output = io.StringIO()
        with redirect_stdout(output):
            print_lesson_order_diagnostics(diagnostics)
        rendered = output.getvalue()
        self.assertIn("pedagogical_monotonicity=fail", rendered)
        self.assertIn("POR2A3 date=2026-09-07", rendered)
        self.assertIn("POR2A4 date=2026-09-05", rendered)


if __name__ == "__main__":
    unittest.main()
