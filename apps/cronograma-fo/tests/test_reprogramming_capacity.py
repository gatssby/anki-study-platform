from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from app.db import initialize_or_migrate_database
from app.reprogramming import (
    LessonCandidate,
    PlannedAssignment,
    PlannedDay,
    ScheduleSettings,
    ScheduleUnavailability,
    apply_reprogramming,
    build_planned_days,
    build_reprogram_report,
    distribute_tracks_with_capacity,
    find_overflow_days,
    lesson_weight_units,
)
from app.web import serialize_report
from scripts.reprogram_schedule import print_report


def settings(target: date, *, include_weekends: bool = True) -> ScheduleSettings:
    return ScheduleSettings(
        target_finish_date=target,
        include_weekends=include_weekends,
        max_daily_minutes_weekday=240,
        max_daily_minutes_saturday=180,
        max_daily_minutes_sunday=180,
    )


def candidate(code: str, minutes: int, *, track: str = "FO", order: int = 1) -> LessonCandidate:
    return LessonCandidate(
        lesson_code=code,
        track_code=track,
        subject_prefix=track,
        group_label=track,
        weight_units=minutes,
        original_order=order,
        current_date=date(2026, 9, 7),
        row={},
    )


class DailyCapacityUnitTest(unittest.TestCase):
    def test_three_weekdays_have_720_minutes_total_capacity(self) -> None:
        days = build_planned_days(settings(date(2026, 9, 9)), date(2026, 9, 7), date(2026, 9, 9), [])
        self.assertEqual([day.capacity_units for day in days], [240, 240, 240])
        self.assertEqual(sum(day.capacity_units for day in days), 720)

    def test_friday_saturday_sunday_have_600_minutes_total_capacity(self) -> None:
        days = build_planned_days(settings(date(2026, 9, 6)), date(2026, 9, 4), date(2026, 9, 6), [])
        self.assertEqual([day.capacity_units for day in days], [240, 180, 180])
        self.assertEqual(sum(day.capacity_units for day in days), 600)

    def test_partial_capacity_applies_percentage_to_daily_minutes(self) -> None:
        unavailable = [ScheduleUnavailability(1, date(2026, 9, 7), date(2026, 9, 7), 50)]
        day = build_planned_days(
            settings(date(2026, 9, 7)), date(2026, 9, 7), date(2026, 9, 7), unavailable
        )[0]
        self.assertEqual(day.capacity_units, 120)
        self.assertTrue(day.is_available)

    def test_unavailable_day_has_zero_capacity(self) -> None:
        unavailable = [ScheduleUnavailability(1, date(2026, 9, 7), date(2026, 9, 7), 0)]
        day = build_planned_days(
            settings(date(2026, 9, 7)), date(2026, 9, 7), date(2026, 9, 7), unavailable
        )[0]
        self.assertEqual(day.capacity_units, 0)
        self.assertFalse(day.is_available)

    def test_daily_overflow_is_detected(self) -> None:
        day = PlannedDay(date(2026, 9, 7), True, 240, 100)
        lesson = candidate("OVER", 300)
        day.assignments.append(PlannedAssignment(lesson, day.date_value))
        self.assertEqual(
            find_overflow_days([day]),
            [{
                "date": "2026-09-07",
                "assigned_units": 300,
                "capacity_units": 240,
                "overflow_units": 60,
            }],
        )

    def test_duration_seconds_are_converted_once_to_minutes(self) -> None:
        self.assertEqual(lesson_weight_units({"duration_seconds": 90 * 60}), 90)
        self.assertEqual(lesson_weight_units({"duration_seconds": 0}), 45)
        self.assertEqual(lesson_weight_units({"track_code": "UN", "duration_seconds": 0}), 10)
        day = build_planned_days(
            settings(date(2026, 9, 7)), date(2026, 9, 7), date(2026, 9, 7), []
        )[0]
        self.assertEqual(day.capacity_units, 240)

    def test_tracks_use_capacity_that_the_other_track_head_cannot_use(self) -> None:
        saturday = PlannedDay(date(2026, 9, 5), True, 180, 100)
        monday = PlannedDay(date(2026, 9, 7), True, 240, 100)
        unallocated = distribute_tracks_with_capacity(
            fo_candidates=[candidate("FO-1", 200, track="FO")],
            un_candidates=[candidate("UN-1", 100, track="UN")],
            planned_days=[saturday, monday],
        )
        self.assertEqual(unallocated, [])
        self.assertEqual([item.lesson.lesson_code for item in saturday.assignments], ["UN-1"])
        self.assertEqual([item.lesson.lesson_code for item in monday.assignments], ["FO-1"])


class ReprogrammingCapacityIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="reprogram-capacity-")
        self.db_path = Path(self.tempdir.name) / "cronograma.db"
        initialize_or_migrate_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def insert_lesson(
        self,
        code: str,
        track: str,
        minutes: int | None,
        order: int,
        *,
        subject: str | None = None,
    ) -> None:
        subject_code = subject or track
        self.conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw, portal_title,
                duration_seconds, subject_name, subject_prefix, module_label, module_number,
                lesson_number, week_number, day_index, day_name, slot_index, recommended_date,
                is_seen, is_cut, cut_reason, source_sheet
            ) VALUES (?, ?, ?, 'lesson', ?, NULL, ?, ?, ?, 'I', 1, ?, 1, 1, 'Segunda', ?, ?, 0, 0, NULL, 'test')
            """,
            (
                f"S-{code}", code, track, f"{subject_code} Aula {order}",
                minutes * 60 if minutes is not None else None,
                subject_code, subject_code, order, order, "2026-09-05",
            ),
        )

    def report_for(self, target: date, as_of: date) -> object:
        self.conn.commit()
        return build_reprogram_report(self.conn, settings(target), as_of_date=as_of)

    def test_feasible_fo_and_un_plan_has_no_deficit_or_daily_overflow(self) -> None:
        self.insert_lesson("FO-1", "FO", 100, 1, subject="MAT")
        self.insert_lesson("UN-1", "UN", 140, 1)
        report = self.report_for(date(2026, 9, 7), date(2026, 9, 7))

        self.assertTrue(report.feasible)
        self.assertEqual(report.total_remaining_units, 240)
        self.assertEqual(report.total_capacity_units, 240)
        self.assertEqual(report.capacity_deficit_units, 0)
        self.assertEqual(report.overflow_days, [])
        self.assertEqual({row["track_code"] for row in report.track_distribution}, {"FO", "UN"})
        payload = serialize_report(report)
        self.assertEqual(payload["load_capacity_unit"], "minutes")
        self.assertEqual(payload["total_capacity_units"], 240)
        self.assertEqual(payload["capacity_deficit_units"], 0)
        self.assertEqual(payload["overflow_days"], [])

    def test_infeasible_plan_reports_deficit_and_apply_aborts_without_changes(self) -> None:
        self.insert_lesson("FO-1", "FO", 200, 1, subject="MAT")
        self.insert_lesson("UN-1", "UN", 100, 1)
        self.conn.commit()
        before = "\n".join(self.conn.iterdump())
        report = build_reprogram_report(
            self.conn, settings(date(2026, 9, 7)), as_of_date=date(2026, 9, 7)
        )

        self.assertFalse(report.feasible)
        self.assertEqual(report.total_remaining_units, 300)
        self.assertEqual(report.total_capacity_units, 240)
        self.assertEqual(report.capacity_deficit_units, 60)
        with self.assertRaisesRegex(ValueError, "Plano inviável"):
            apply_reprogramming(
                self.conn,
                settings(date(2026, 9, 7)),
                as_of_date=date(2026, 9, 7),
                db_path=self.db_path,
            )
        self.assertEqual("\n".join(self.conn.iterdump()), before)
        self.assertFalse((self.db_path.parent / "backups").exists())

    def test_zero_total_capacity_can_never_be_reported_as_feasible(self) -> None:
        self.insert_lesson("FO-1", "FO", 60, 1, subject="MAT")
        self.conn.execute(
            """
            INSERT INTO schedule_unavailability (start_date, end_date, capacity_percent, reason)
            VALUES ('2026-09-07', '2026-09-07', 0, 'fixture')
            """
        )
        report = self.report_for(date(2026, 9, 7), date(2026, 9, 7))

        self.assertEqual(report.total_capacity_units, 0)
        self.assertEqual(report.capacity_deficit_units, 60)
        self.assertFalse(report.feasible)
        self.assertEqual(report.assignment_count, 0)

    def test_shared_and_standalone_fo_unallocated_counts_are_explicit(self) -> None:
        for index in range(1, 4):
            self.insert_lesson(f"FO-{index}", "FO", 80, index, subject="MAT")
        for index in range(1, 3):
            self.insert_lesson(f"UN-{index}", "UN", 120, index)
        report = self.report_for(date(2026, 9, 7), date(2026, 9, 7))

        self.assertEqual(report.fo_plan_summary["scope"], "standalone_fo_only_without_un_capacity_competition")
        self.assertEqual(report.fo_plan_summary["standalone_unallocated_lesson_count"], 0)
        self.assertEqual(report.unallocated_lesson_count_by_track, {"FO": 2, "UN": 1})
        self.assertTrue(
            any("FO=2 e UN=1" in error for error in report.validation_errors)
        )

        output = io.StringIO()
        with redirect_stdout(output):
            print_report(report, mode="dry-run")
        rendered = output.getvalue()
        self.assertIn("aulas_nao_alocadas_FO: 2", rendered)
        self.assertIn("diagnostico_fo_isolado_sem_competicao_UN_nao_alocadas: 0", rendered)

    def test_duration_diagnostics_separate_real_and_fallback_minutes_by_track(self) -> None:
        self.insert_lesson("FO-REAL", "FO", 60, 1, subject="MAT")
        self.insert_lesson("FO-FALLBACK", "FO", None, 2, subject="MAT")
        self.insert_lesson("UN-REAL", "UN", 8, 1)
        self.insert_lesson("UN-FALLBACK", "UN", None, 2)
        report = self.report_for(date(2026, 9, 7), date(2026, 9, 7))

        self.assertEqual(
            report.duration_diagnostics["FO"],
            {
                "real_duration_lesson_count": 1,
                "fallback_lesson_count": 1,
                "real_duration_minutes": 60,
                "fallback_minutes": 45,
                "fallback_minutes_per_lesson": 45,
            },
        )
        self.assertEqual(
            report.duration_diagnostics["UN"],
            {
                "real_duration_lesson_count": 1,
                "fallback_lesson_count": 1,
                "real_duration_minutes": 8,
                "fallback_minutes": 10,
                "fallback_minutes_per_lesson": 10,
            },
        )

    def test_por2_diagnostic_lists_seen_cut_and_projected_lessons_in_order(self) -> None:
        durations = [55, 10, 50, 15, 45, 20, 40, 25, 35, 30]
        for index, minutes in enumerate(durations, start=1):
            self.insert_lesson(f"POR2A{index}", "FO", minutes, index, subject="POR")
        self.conn.execute("UPDATE lessons SET is_seen=1 WHERE lesson_code='POR2A1'")
        self.conn.execute(
            "UPDATE lessons SET is_cut=1, cut_source='manual', cut_reason='fixture' "
            "WHERE lesson_code='POR2A2'"
        )
        self.conn.commit()
        report = build_reprogram_report(
            self.conn,
            settings(date(2026, 9, 9)),
            as_of_date=date(2026, 9, 7),
            diagnostic_lesson_prefixes=("POR2",),
        )

        diagnostic = report.lesson_order_diagnostics[0]
        self.assertTrue(diagnostic["is_valid"])
        self.assertEqual(
            [(entry["lesson_code"], entry["status"]) for entry in diagnostic["entries"]],
            [("POR2A1", "seen"), ("POR2A2", "cut")]
            + [(f"POR2A{index}", "projected") for index in range(3, 11)],
        )
        projected_entries = [
            entry for entry in diagnostic["entries"] if entry["status"] == "projected"
        ]
        self.assertEqual(
            [entry["lesson_code"] for entry in projected_entries],
            [f"POR2A{index}" for index in range(3, 11)],
        )
        self.assertEqual(
            [entry["projected_date"] for entry in projected_entries],
            sorted(entry["projected_date"] for entry in projected_entries),
        )

    def test_current_shape_fixture_reports_real_capacity_and_infeasibility(self) -> None:
        for index in range(1, 112):
            self.insert_lesson(f"FO-{index:03d}", "FO", 180, index, subject="MAT")
        self.insert_lesson("FO-112", "FO", 120, 112, subject="MAT")
        for index in range(1, 83):
            self.insert_lesson(f"UN-{index:03d}", "UN", 180, index)
        self.insert_lesson("UN-083", "UN", 14, 83)

        report = self.report_for(date(2026, 10, 29), date(2026, 9, 5))

        self.assertEqual(report.remaining_units_by_track, {"FO": 20_100, "UN": 14_774})
        self.assertEqual(report.total_remaining_units, 34_874)
        self.assertEqual(report.total_capacity_units, 12_240)
        self.assertEqual(report.capacity_deficit_units, 22_634)
        self.assertFalse(report.feasible)
        self.assertEqual(report.overflow_days, [])
        self.assertTrue(
            all(day.assigned_units <= day.capacity_units for day in report.available_days)
        )

        output = io.StringIO()
        with redirect_stdout(output):
            print_report(report, mode="dry-run")
        rendered = output.getvalue()
        self.assertIn("unidade_carga_capacidade: minutos", rendered)
        self.assertIn("carga_total_disponivel: 12240", rendered)
        self.assertIn("deficit: 22634", rendered)
        self.assertIn("status: inviavel", rendered)
        self.assertIn("dias_acima_do_teto: 0", rendered)


if __name__ == "__main__":
    unittest.main()
