from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from app.dashboard import build_today_rows
from scripts.validate_fo_daily_assignments import validate_daily_assignments


SCHEMA = """
CREATE TABLE lessons (
    slot_key TEXT PRIMARY KEY,
    lesson_code TEXT NOT NULL UNIQUE,
    track_code TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    title_raw TEXT NOT NULL,
    subject_prefix TEXT,
    module_label TEXT,
    module_number INTEGER,
    recommended_date TEXT NOT NULL,
    day_index INTEGER NOT NULL,
    slot_index INTEGER NOT NULL,
    is_seen INTEGER NOT NULL DEFAULT 0,
    is_cut INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE daily_assignments (
    dashboard_date TEXT NOT NULL,
    planned_slot_key TEXT NOT NULL,
    assigned_lesson_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dashboard_date, planned_slot_key)
);
"""


class FoDailyAssignmentValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def tearDown(self) -> None:
        self.conn.close()

    def insert_lesson(
        self,
        code: str,
        slot: str,
        *,
        subject: str = "MAT",
        module: int | None = 1,
        recommended_date: str = "2026-07-09",
        track: str = "FO",
        lesson_type: str = "lesson",
        is_seen: int = 0,
        is_cut: int = 0,
        slot_index: int = 1,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw,
                subject_prefix, module_label, module_number, recommended_date,
                day_index, slot_index, is_seen, is_cut
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 4, ?, ?, ?)
            """,
            (
                slot,
                code,
                track,
                lesson_type,
                code,
                subject,
                f"Modulo {module}" if module is not None else None,
                module,
                recommended_date,
                slot_index,
                is_seen,
                is_cut,
            ),
        )

    def assign(self, date_value: str, slot: str, code: str) -> None:
        self.conn.execute(
            """
            INSERT INTO daily_assignments (
                dashboard_date, planned_slot_key, assigned_lesson_code
            ) VALUES (?, ?, ?)
            """,
            (date_value, slot, code),
        )

    def assert_error_contains(self, expected: str) -> None:
        report = self.validate()
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any(expected in error for error in report.errors),
            msg=f"Erro esperado ausente: {expected}; erros={report.errors}",
        )

    def validate(self):
        return validate_daily_assignments(
            self.conn,
            as_of_date=date(2026, 7, 9),
        )

    def test_assignment_equal_to_planned_slot_is_valid(self) -> None:
        self.insert_lesson("MAT1A2", "W01-D4-S1")
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A2")

        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual((report.exact_count, report.substitution_count), (1, 0))

    def test_valid_substitution_created_by_home_is_accepted(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            recommended_date="2026-07-08",
            slot_index=1,
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1", slot_index=1)

        rows, _ = build_today_rows(self.conn, target_date="2026-07-09")
        report = self.validate()

        self.assertEqual(rows[0]["target"]["lesson_code"], "MAT1A1")
        self.assertTrue(report.is_valid)
        self.assertEqual((report.exact_count, report.substitution_count), (0, 1))

    def test_missing_assigned_lesson_is_rejected(self) -> None:
        self.insert_lesson("MAT1A2", "W01-D4-S1")
        self.assign("2026-07-09", "W01-D4-S1", "MISSING")

        self.assert_error_contains("aula atribuida inexistente")

    def test_missing_planned_slot_is_rejected(self) -> None:
        self.insert_lesson("MAT1A1", "W01-D3-S1", recommended_date="2026-07-08")
        self.assign("2026-07-09", "MISSING-SLOT", "MAT1A1")

        self.assert_error_contains("slot planejado inexistente")

    def test_cut_assigned_lesson_is_rejected(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            recommended_date="2026-07-08",
            is_cut=1,
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1")
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A1")

        self.assert_error_contains("aula atribuida esta cortada")

    def test_cut_planned_slot_is_rejected(self) -> None:
        self.insert_lesson("MAT1A2", "W01-D4-S1", is_cut=1)
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A2")

        self.assert_error_contains("slot planejado esta cortado")

    def test_assigned_lesson_from_un_is_rejected(self) -> None:
        self.insert_lesson(
            "UN-MAT-1",
            "UN-S1",
            track="UN",
            recommended_date="2026-07-08",
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1")
        self.assign("2026-07-09", "W01-D4-S1", "UN-MAT-1")

        self.assert_error_contains("aula atribuida nao pertence a FO")

    def test_incompatible_subject_or_module_is_rejected(self) -> None:
        self.insert_lesson(
            "MAT2A1",
            "W01-D3-S1",
            subject="MAT",
            module=2,
            recommended_date="2026-07-08",
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1", subject="MAT")
        self.assign("2026-07-09", "W01-D4-S1", "MAT2A1")

        self.assert_error_contains("substituicao incompatível com disciplina/modulo")

    def test_future_assigned_lesson_is_rejected(self) -> None:
        self.insert_lesson(
            "MAT1A3",
            "W01-D5-S1",
            recommended_date="2026-07-10",
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1")
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A3")

        self.assert_error_contains("ainda nao era elegivel na data exibida")

    def test_historical_assignment_allows_later_schedule_date_changes(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            recommended_date="2026-07-13",
        )
        self.insert_lesson(
            "MAT1A2",
            "W01-D4-S1",
            recommended_date="2026-07-12",
        )
        self.assign("2026-07-08", "W01-D4-S1", "MAT1A1")

        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual(report.substitution_count, 1)

    def test_incompatible_duplicate_assignment_is_rejected(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            subject="MAT",
            recommended_date="2026-07-08",
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1", subject="MAT", slot_index=1)
        self.insert_lesson("FIS1A2", "W01-D4-S2", subject="FIS", slot_index=2)
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A1")
        self.assign("2026-07-09", "W01-D4-S2", "MAT1A1")

        self.assert_error_contains("reutilizada em slots incompatíveis")

    def test_compatible_duplicate_created_by_home_is_accepted(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            recommended_date="2026-07-08",
            slot_index=1,
        )
        self.insert_lesson("MAT1A2", "W01-D4-S1", slot_index=1)
        self.insert_lesson("MAT1A3", "W01-D4-S2", slot_index=2)

        build_today_rows(self.conn, target_date="2026-07-09")
        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual(report.substitution_count, 2)
        self.assertEqual(report.compatible_duplicate_count, 1)

    def test_database_without_assignments_is_valid(self) -> None:
        self.insert_lesson("MAT1A2", "W01-D4-S1")

        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual(report.total_count, 0)


if __name__ == "__main__":
    unittest.main()
