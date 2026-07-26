from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db import (
    RuntimeDatabaseError,
    connect_db,
    init_db,
    initialize_or_migrate_database,
    inspect_database,
)
from app.web import create_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_SCRIPT = PROJECT_ROOT / "scripts" / "init_or_migrate_db.py"

LEGACY_LESSONS_SCHEMA = """
CREATE TABLE lessons (
    slot_key TEXT PRIMARY KEY,
    lesson_code TEXT NOT NULL UNIQUE,
    track_code TEXT NOT NULL DEFAULT 'FO',
    lesson_type TEXT NOT NULL CHECK (lesson_type IN ('lesson', 'pending', 'review')),
    title_raw TEXT NOT NULL,
    relative_path TEXT,
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
    source_sheet TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

EXERCISE_TASKS_SCHEMA = """
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
"""


def file_state(path: Path) -> tuple[int, int, str, bytes]:
    stat = path.stat()
    content = path.read_bytes()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(content).hexdigest(), content


class DatabaseBootMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "cronograma.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_current_database(self) -> None:
        _, after = initialize_or_migrate_database(self.db_path)
        self.assertTrue(after.is_runtime_compatible, after.problems)

    def create_legacy_database(self, *, with_exercise: bool = False) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(LEGACY_LESSONS_SCHEMA)
        conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw,
                relative_path, subject_name, subject_prefix, module_label,
                module_number, lesson_number, week_number, day_index, day_name,
                slot_index, recommended_date, is_seen, seen_at, source_sheet,
                created_at, updated_at
            ) VALUES (
                'W01-D1-S1', 'MAT1A1', 'FO', 'lesson', 'Matematica - Aula 1',
                'mat/aula-1', 'Matematica', 'MAT', 'Modulo 1',
                1, 1, 1, 1, 'Segunda', 1, '2026-03-02', 1,
                '2026-03-02 10:00:00', 'legacy',
                '2026-03-01 08:00:00', '2026-03-02 10:00:00'
            )
            """
        )
        if with_exercise:
            conn.executescript(EXERCISE_TASKS_SCHEMA)
            conn.execute(
                """
                INSERT INTO exercise_tasks (
                    source_lesson_code, scheduled_date, status, is_active,
                    manually_moved
                ) VALUES ('MAT1A1', '2026-03-05', 'done', 0, 1)
                """
            )
        conn.commit()
        conn.close()

    def run_migration_command(self, mode: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [
                sys.executable,
                str(MIGRATION_SCRIPT),
                "--db",
                str(self.db_path),
                mode,
            ],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_current_database_boot_validates_without_writing(self) -> None:
        self.create_current_database()
        before = file_state(self.db_path)

        app = create_app(self.db_path)

        self.assertIsNotNone(app)
        self.assertEqual(before, file_state(self.db_path))

    def test_missing_database_boot_fails_without_creating_file(self) -> None:
        with self.assertRaisesRegex(RuntimeDatabaseError, "banco inexistente"):
            create_app(self.db_path)

        self.assertFalse(self.db_path.exists())

    def test_incomplete_schema_boot_fails_without_altering_database(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE lessons (slot_key TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        before = file_state(self.db_path)

        with self.assertRaisesRegex(RuntimeDatabaseError, "colunas ausentes"):
            create_app(self.db_path)

        self.assertEqual(before, file_state(self.db_path))

    def test_check_detects_legacy_schema_without_writing(self) -> None:
        self.create_legacy_database()
        before = file_state(self.db_path)

        result = self.run_migration_command("--check")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("reconstruir lessons com a constraint atual", result.stdout)
        self.assertIn("check_result=migration_required", result.stdout)
        self.assertEqual(before, file_state(self.db_path))

    def test_legacy_schema_cannot_migrate_without_explicit_flow(self) -> None:
        self.create_legacy_database()
        before = file_state(self.db_path)
        conn = connect_db(self.db_path)
        try:
            with self.assertRaisesRegex(RuntimeDatabaseError, "migracao explicita"):
                init_db(conn)
        finally:
            conn.close()

        self.assertEqual(before, file_state(self.db_path))

    def test_explicit_apply_migrates_and_second_apply_is_idempotent(self) -> None:
        self.create_legacy_database()

        first = self.run_migration_command("--apply")
        after_first = file_state(self.db_path)
        second = self.run_migration_command("--apply")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("apply_result=ok", first.stdout)
        self.assertTrue(inspect_database(self.db_path).is_runtime_compatible)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(after_first, file_state(self.db_path))

    def test_legacy_lessons_migration_preserves_lesson_and_dependents(self) -> None:
        self.create_legacy_database(with_exercise=True)

        _, after = initialize_or_migrate_database(self.db_path)

        self.assertTrue(after.is_runtime_compatible, after.problems)
        conn = sqlite3.connect(self.db_path)
        lesson = conn.execute(
            """
            SELECT lesson_code, title_raw, relative_path, is_seen, seen_at,
                   is_cut, portal_title, external_url, duration_seconds
            FROM lessons WHERE lesson_code = 'MAT1A1'
            """
        ).fetchone()
        exercise = conn.execute(
            """
            SELECT source_lesson_code, scheduled_date, status, manually_moved
            FROM exercise_tasks WHERE source_lesson_code = 'MAT1A1'
            """
        ).fetchone()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        exercise_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='exercise_tasks'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(
            lesson,
            (
                "MAT1A1", "Matematica - Aula 1", "mat/aula-1", 1,
                "2026-03-02 10:00:00", 0, None, None, None,
            ),
        )
        self.assertEqual(exercise, ("MAT1A1", "2026-03-05", "done", 1))
        self.assertEqual(foreign_keys, [])
        self.assertIn("REFERENCES lessons(lesson_code)", exercise_sql)

    def test_two_simultaneous_boots_do_not_modify_database(self) -> None:
        self.create_current_database()
        before = file_state(self.db_path)

        with ThreadPoolExecutor(max_workers=2) as executor:
            apps = list(executor.map(lambda _: create_app(self.db_path), range(2)))

        self.assertEqual(len(apps), 2)
        self.assertEqual(before, file_state(self.db_path))

    def test_boot_preserves_size_mtime_hash_and_database_content(self) -> None:
        self.create_current_database()
        conn = sqlite3.connect(self.db_path)
        before_dump = "\n".join(conn.iterdump())
        conn.close()
        before = file_state(self.db_path)

        create_app(self.db_path)

        conn = sqlite3.connect(self.db_path)
        after_dump = "\n".join(conn.iterdump())
        conn.close()
        self.assertEqual(before, file_state(self.db_path))
        self.assertEqual(before_dump, after_dump)


if __name__ == "__main__":
    unittest.main()
