from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from app.dashboard import (
    DuplicateDailyAssignmentError,
    build_today_rows,
    first_unseen_excluding,
)
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

    def insert_bio_queue(
        self,
        *,
        recommended_date: str = "2026-07-09",
    ) -> None:
        for lesson_number in range(1, 7):
            self.insert_lesson(
                f"BIO1A{lesson_number}",
                f"W01-D{lesson_number}-S1",
                subject="BIO",
                recommended_date="2026-07-08",
                is_seen=1,
                slot_index=lesson_number,
            )
        self.insert_lesson(
            "BIO1A7",
            "W04-D1-S1",
            subject="BIO",
            recommended_date=recommended_date,
            slot_index=1,
        )
        self.insert_lesson(
            "BIO1A8",
            "W04-D5-S2",
            subject="BIO",
            recommended_date=recommended_date,
            slot_index=2,
        )

    def test_first_unseen_excludes_reserved_codes(self) -> None:
        lessons = [
            {"lesson_code": "BIO1A6", "is_seen": 1},
            {"lesson_code": "BIO1A7", "is_seen": 0},
            {"lesson_code": "BIO1A8", "is_seen": 0},
        ]

        selected = first_unseen_excluding(
            lessons,
            excluded_codes={"BIO1A7"},
        )

        self.assertEqual(selected["lesson_code"], "BIO1A8")

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

        rows, _ = build_today_rows(
            self.conn,
            target_date="2026-07-09",
            as_of_date=date(2026, 7, 9),
        )
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

        self.assert_error_contains("assigned_lesson_code duplicado na mesma data")

    def test_same_scope_slots_assign_distinct_pending_lessons(self) -> None:
        self.insert_bio_queue()

        rows, _ = build_today_rows(
            self.conn,
            target_date="2026-07-09",
            as_of_date=date(2026, 7, 9),
        )
        assignments = self.conn.execute(
            """
            SELECT planned_slot_key, assigned_lesson_code
            FROM daily_assignments
            WHERE dashboard_date = '2026-07-09'
            ORDER BY planned_slot_key
            """
        ).fetchall()

        self.assertEqual(
            [row["target"]["lesson_code"] for row in rows],
            ["BIO1A7", "BIO1A8"],
        )
        self.assertEqual(
            [(row["planned_slot_key"], row["assigned_lesson_code"]) for row in assignments],
            [("W04-D1-S1", "BIO1A7"), ("W04-D5-S2", "BIO1A8")],
        )
        self.assertIsNone(rows[0]["warning"])
        self.assertIsNone(rows[1]["warning"])

    def test_existing_distinct_assignments_are_preserved(self) -> None:
        self.insert_bio_queue()
        self.assign("2026-07-09", "W04-D1-S1", "BIO1A7")
        self.assign("2026-07-09", "W04-D5-S2", "BIO1A8")
        before = self.conn.execute(
            "SELECT * FROM daily_assignments ORDER BY planned_slot_key"
        ).fetchall()

        rows, _ = build_today_rows(
            self.conn,
            target_date="2026-07-09",
            as_of_date=date(2026, 7, 9),
        )
        after = self.conn.execute(
            "SELECT * FROM daily_assignments ORDER BY planned_slot_key"
        ).fetchall()

        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])
        self.assertEqual(
            [row["target"]["lesson_code"] for row in rows],
            ["BIO1A7", "BIO1A8"],
        )

    def test_existing_duplicate_assignment_is_rematerialized_for_today(self) -> None:
        self.insert_bio_queue()
        self.assign("2026-07-09", "W04-D1-S1", "BIO1A7")
        self.assign("2026-07-09", "W04-D5-S2", "BIO1A7")

        build_today_rows(
            self.conn,
            target_date="2026-07-09",
            as_of_date=date(2026, 7, 9),
        )
        assignments = self.conn.execute(
            """
            SELECT planned_slot_key, assigned_lesson_code
            FROM daily_assignments
            ORDER BY planned_slot_key
            """
        ).fetchall()

        self.assertEqual(
            [(row["planned_slot_key"], row["assigned_lesson_code"]) for row in assignments],
            [("W04-D1-S1", "BIO1A7"), ("W04-D5-S2", "BIO1A8")],
        )

    def test_historical_duplicate_is_not_rewritten(self) -> None:
        self.insert_bio_queue(recommended_date="2026-07-08")
        self.assign("2026-07-08", "W04-D1-S1", "BIO1A7")
        self.assign("2026-07-08", "W04-D5-S2", "BIO1A7")
        before = self.conn.execute(
            "SELECT * FROM daily_assignments ORDER BY planned_slot_key"
        ).fetchall()

        build_today_rows(
            self.conn,
            target_date="2026-07-08",
            as_of_date=date(2026, 7, 9),
        )
        after = self.conn.execute(
            "SELECT * FROM daily_assignments ORDER BY planned_slot_key"
        ).fetchall()

        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])

    def test_validator_rejects_duplicate_assigned_lesson_on_same_date(self) -> None:
        self.insert_bio_queue()
        self.assign("2026-07-09", "W04-D1-S1", "BIO1A7")
        self.assign("2026-07-09", "W04-D5-S2", "BIO1A7")

        report = self.validate()

        self.assertFalse(report.is_valid)
        self.assertEqual(report.duplicate_count, 1)
        self.assertEqual(report.historical_duplicate_count, 0)
        self.assertTrue(
            any("assigned_lesson_code duplicado na mesma data" in error for error in report.errors)
        )

    def test_assignment_transaction_rolls_back_on_duplicate(self) -> None:
        self.insert_bio_queue()
        self.conn.commit()

        def colliding_upsert(
            conn: sqlite3.Connection,
            target_date: str,
            planned_slot_key: str,
            assigned_lesson_code: str,
        ) -> None:
            del assigned_lesson_code
            conn.execute(
                """
                INSERT INTO daily_assignments (
                    dashboard_date, planned_slot_key, assigned_lesson_code
                ) VALUES (?, ?, 'BIO1A7')
                ON CONFLICT(dashboard_date, planned_slot_key) DO UPDATE SET
                    assigned_lesson_code = excluded.assigned_lesson_code
                """,
                (target_date, planned_slot_key),
            )

        with patch("app.dashboard.upsert_assignment", side_effect=colliding_upsert):
            with self.assertRaises(DuplicateDailyAssignmentError):
                build_today_rows(
                    self.conn,
                    target_date="2026-07-09",
                    as_of_date=date(2026, 7, 9),
                )

        assignment_count = self.conn.execute(
            "SELECT COUNT(*) FROM daily_assignments"
        ).fetchone()[0]
        self.assertEqual(assignment_count, 0)

    def test_same_lesson_can_exist_on_different_dates_when_semantically_allowed(self) -> None:
        self.insert_lesson(
            "MAT1A1",
            "W01-D3-S1",
            recommended_date="2026-07-08",
        )
        self.insert_lesson(
            "MAT1A2",
            "W01-D4-S1",
            recommended_date="2026-07-09",
        )
        self.insert_lesson(
            "MAT1A3",
            "W01-D5-S1",
            recommended_date="2026-07-10",
        )
        self.assign("2026-07-09", "W01-D4-S1", "MAT1A1")
        self.assign("2026-07-10", "W01-D5-S1", "MAT1A1")

        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual(report.substitution_count, 2)
        self.assertEqual(report.duplicate_count, 0)
        self.assertEqual(report.historical_duplicate_count, 0)

    def test_database_without_assignments_is_valid(self) -> None:
        self.insert_lesson("MAT1A2", "W01-D4-S1")

        report = self.validate()

        self.assertTrue(report.is_valid)
        self.assertEqual(report.total_count, 0)


if __name__ == "__main__":
    unittest.main()
