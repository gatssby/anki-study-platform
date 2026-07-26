from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from app.importer import (
    FoImportPreflightError,
    ParsedLesson,
    build_fo_import_preflight,
    import_fo_lessons,
)


TEST_SCHEMA = """
CREATE TABLE lessons (
    slot_key TEXT PRIMARY KEY,
    lesson_code TEXT NOT NULL UNIQUE,
    track_code TEXT NOT NULL DEFAULT 'FO',
    lesson_type TEXT NOT NULL,
    title_raw TEXT NOT NULL,
    portal_title TEXT,
    relative_path TEXT,
    external_url TEXT,
    duration_seconds INTEGER,
    subject_name TEXT,
    subject_prefix TEXT,
    module_label TEXT,
    module_number INTEGER,
    lesson_number INTEGER,
    week_number INTEGER NOT NULL,
    day_index INTEGER NOT NULL,
    day_name TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    recommended_date TEXT NOT NULL,
    is_seen INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT,
    is_cut INTEGER NOT NULL DEFAULT 0,
    cut_reason TEXT,
    cut_source TEXT,
    source_sheet TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE daily_assignments (
    dashboard_date TEXT NOT NULL,
    planned_slot_key TEXT NOT NULL,
    assigned_lesson_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dashboard_date, planned_slot_key)
);
CREATE TABLE exercise_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_lesson_code TEXT NOT NULL UNIQUE,
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    is_active INTEGER NOT NULL DEFAULT 0,
    manually_moved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_lesson_code) REFERENCES lessons(lesson_code) ON DELETE CASCADE
);
CREATE TABLE app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def lesson(
    code: str = "FIS1A1",
    slot: str = "W01-D1-S1",
    recommended_date: str = "2026-03-02",
    title: str = "Física I - Aula 1",
) -> ParsedLesson:
    return ParsedLesson(
        slot_key=slot,
        lesson_code=code,
        track_code="FO",
        lesson_type="lesson",
        title_raw=title,
        portal_title=f"Portal {title}",
        relative_path=None,
        external_url="https://example.invalid/video",
        duration_seconds=1800,
        subject_name="Física",
        subject_prefix="FIS",
        module_label="I",
        module_number=1,
        lesson_number=int(code.rsplit("A", 1)[1]),
        week_number=1,
        day_index=1,
        day_name="Segunda",
        slot_index=1,
        recommended_date=recommended_date,
        source_sheet="02mar (30S)",
    )


class SafeFoImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cronograma-fo-import-test-")
        self.db_path = Path(self.temp_dir.name) / "cronograma.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(TEST_SCHEMA)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def insert_existing(
        self,
        item: ParsedLesson,
        *,
        recommended_date: str | None = None,
        is_seen: int = 0,
        seen_at: str | None = None,
        is_cut: int = 0,
        cut_reason: str | None = None,
        cut_source: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw, portal_title,
                relative_path, external_url, duration_seconds, subject_name, subject_prefix,
                module_label, module_number, lesson_number, week_number, day_index, day_name,
                slot_index, recommended_date, is_seen, seen_at, is_cut, cut_reason, cut_source,
                source_sheet
            ) VALUES (?, ?, 'FO', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.slot_key,
                item.lesson_code,
                item.lesson_type,
                item.title_raw,
                item.portal_title,
                item.relative_path,
                item.external_url,
                item.duration_seconds,
                item.subject_name,
                item.subject_prefix,
                item.module_label,
                item.module_number,
                item.lesson_number,
                item.week_number,
                item.day_index,
                item.day_name,
                item.slot_index,
                recommended_date or item.recommended_date,
                is_seen,
                seen_at,
                is_cut,
                cut_reason,
                cut_source,
                item.source_sheet,
            ),
        )
        self.conn.commit()

    def insert_exercise(
        self,
        code: str,
        *,
        status: str = "done",
        scheduled_date: str = "2026-04-20",
        is_active: int = 1,
        manually_moved: int = 1,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO exercise_tasks (
                source_lesson_code, scheduled_date, status, is_active, manually_moved
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (code, scheduled_date, status, is_active, manually_moved),
        )
        self.conn.commit()

    def protected_state(self, code: str) -> tuple:
        lesson_row = self.conn.execute(
            """
            SELECT is_seen, seen_at, is_cut, cut_reason, cut_source, recommended_date,
                   week_number, day_index, day_name, slot_index
            FROM lessons WHERE lesson_code = ?
            """,
            (code,),
        ).fetchone()
        exercise_row = self.conn.execute(
            """
            SELECT scheduled_date, status, is_active, manually_moved
            FROM exercise_tasks WHERE source_lesson_code = ?
            """,
            (code,),
        ).fetchone()
        return tuple(lesson_row), tuple(exercise_row) if exercise_row else None

    def database_dump(self) -> str:
        return "\n".join(self.conn.iterdump())

    def test_identical_reimport_preserves_history_and_exercise(self) -> None:
        item = lesson()
        self.insert_existing(
            item,
            is_seen=1,
            seen_at="2026-03-10 20:00:00",
            is_cut=1,
            cut_reason="manual-test",
            cut_source="manual",
        )
        self.insert_exercise(item.lesson_code)
        before = self.protected_state(item.lesson_code)

        result = import_fo_lessons(self.conn, [item])

        self.assertEqual(result.updated_count, 1)
        self.assertEqual(before, self.protected_state(item.lesson_code))

    def test_seen_lesson_keeps_seen_and_cut_fields(self) -> None:
        item = lesson()
        self.insert_existing(
            item,
            is_seen=1,
            seen_at="2026-03-11 08:30:00",
            is_cut=1,
            cut_reason="baixo retorno",
            cut_source="manual",
        )

        import_fo_lessons(self.conn, [replace(item, title_raw="Física I - Aula 1 atualizada")])
        row = self.conn.execute("SELECT * FROM lessons WHERE lesson_code = ?", (item.lesson_code,)).fetchone()

        self.assertEqual(row["title_raw"], "Física I - Aula 1 atualizada")
        self.assertEqual(row["is_seen"], 1)
        self.assertEqual(row["seen_at"], "2026-03-11 08:30:00")
        self.assertEqual(row["is_cut"], 1)
        self.assertEqual(row["cut_reason"], "baixo retorno")
        self.assertEqual(row["cut_source"], "manual")

    def test_adaptive_schedule_is_default_and_replacement_is_explicit(self) -> None:
        item = lesson(recommended_date="2026-03-02")
        self.insert_existing(item, recommended_date="2026-08-15")
        self.conn.execute(
            "INSERT INTO daily_assignments (dashboard_date, planned_slot_key, assigned_lesson_code) VALUES (?, ?, ?)",
            ("2026-08-15", item.slot_key, item.lesson_code),
        )
        self.conn.commit()

        import_fo_lessons(self.conn, [item])
        self.assertEqual(
            self.conn.execute("SELECT recommended_date FROM lessons").fetchone()[0],
            "2026-08-15",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_assignments").fetchone()[0], 1)

        import_fo_lessons(self.conn, [item], replace_schedule_dates=True)
        self.assertEqual(
            self.conn.execute("SELECT recommended_date FROM lessons").fetchone()[0],
            "2026-03-02",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_assignments").fetchone()[0], 0)

    def test_moved_lesson_aborts_before_writing(self) -> None:
        current = lesson(slot="W01-D1-S1")
        moved = replace(current, slot_key="W02-D2-S1")
        self.insert_existing(current, is_seen=1, seen_at="2026-03-03 10:00:00")
        before = self.database_dump()

        with self.assertRaisesRegex(FoImportPreflightError, "aula movida"):
            import_fo_lessons(self.conn, [moved])

        self.assertEqual(before, self.database_dump())

    def test_occupied_slot_aborts_before_writing(self) -> None:
        current = lesson(code="FIS1A1", slot="W01-D1-S1")
        replacement = lesson(code="FIS1A2", slot="W01-D1-S1", title="Física I - Aula 2")
        self.insert_existing(current, is_seen=1, seen_at="2026-03-03 10:00:00")
        before = self.database_dump()

        with self.assertRaisesRegex(FoImportPreflightError, "slot ocupado"):
            import_fo_lessons(self.conn, [replacement])

        self.assertEqual(before, self.database_dump())

    def test_new_lesson_starts_clean_and_gets_new_exercise(self) -> None:
        existing = lesson(code="FIS1A1", slot="W01-D1-S1")
        new_item = lesson(code="FIS1A2", slot="W01-D1-S2", title="Física I - Aula 2")
        self.insert_existing(existing, is_seen=1, seen_at="2026-03-03 10:00:00")
        self.insert_exercise(existing.lesson_code)
        existing_before = self.protected_state(existing.lesson_code)

        result = import_fo_lessons(self.conn, [existing, new_item])

        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(existing_before, self.protected_state(existing.lesson_code))
        new_row = self.conn.execute("SELECT * FROM lessons WHERE lesson_code = ?", (new_item.lesson_code,)).fetchone()
        new_task = self.conn.execute(
            "SELECT * FROM exercise_tasks WHERE source_lesson_code = ?", (new_item.lesson_code,)
        ).fetchone()
        self.assertEqual((new_row["is_seen"], new_row["is_cut"]), (0, 0))
        self.assertEqual(new_task["status"], "pending")
        self.assertEqual(new_task["is_active"], 0)
        self.assertEqual(new_task["manually_moved"], 0)
        self.assertEqual(new_task["scheduled_date"], "2026-03-05")

    def test_missing_lesson_and_exercise_are_preserved(self) -> None:
        retained = lesson(code="FIS1A1", slot="W01-D1-S1")
        missing = lesson(code="FIS1A2", slot="W01-D1-S2", title="Física I - Aula 2")
        self.insert_existing(retained)
        self.insert_existing(missing, is_seen=1, seen_at="2026-03-04 09:00:00")
        self.insert_exercise(missing.lesson_code, status="skipped")
        missing_before = self.protected_state(missing.lesson_code)

        report = build_fo_import_preflight(self.conn, [retained])
        result = import_fo_lessons(self.conn, [retained])

        self.assertEqual(report.missing_lesson_codes, (missing.lesson_code,))
        self.assertEqual(result.preserved_missing_count, 1)
        self.assertEqual(missing_before, self.protected_state(missing.lesson_code))


if __name__ == "__main__":
    unittest.main()
