from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from app.db import connect_db, init_db
from app.exercises import (
    default_task_date_for_lesson,
    get_exercise_offset_days,
    sync_fo_exercise_tasks,
)
from app.web import create_app


def legacy_sync_fo_exercise_tasks(conn: sqlite3.Connection) -> int:
    """Implementação anterior, preservada como oráculo de equivalência."""
    offset_days = get_exercise_offset_days(conn)
    lesson_rows = conn.execute(
        """
        SELECT lesson_code, recommended_date, is_seen, seen_at
        FROM lessons
        WHERE track_code = 'FO'
          AND lesson_type = 'lesson'
        ORDER BY recommended_date, day_index, slot_index
        """
    ).fetchall()

    touched = 0
    for lesson in lesson_rows:
        existing = conn.execute(
            "SELECT id, is_active, manually_moved FROM exercise_tasks WHERE source_lesson_code = ?",
            (lesson["lesson_code"],),
        ).fetchone()
        active = 1 if lesson["is_seen"] == 1 else 0

        if existing is None:
            conn.execute(
                """
                INSERT INTO exercise_tasks (
                    source_lesson_code, scheduled_date, status, is_active,
                    manually_moved, updated_at
                ) VALUES (?, ?, 'pending', ?, 0, CURRENT_TIMESTAMP)
                """,
                (lesson["lesson_code"], default_task_date_for_lesson(lesson, offset_days), active),
            )
            touched += 1
            continue

        if lesson["is_seen"] == 0 and existing["is_active"] == 1:
            conn.execute(
                """
                UPDATE exercise_tasks
                SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (existing["id"],),
            )
            touched += 1
        elif lesson["is_seen"] == 1 and existing["is_active"] == 0:
            conn.execute(
                """
                UPDATE exercise_tasks
                SET is_active = 1, scheduled_date = ?, manually_moved = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (default_task_date_for_lesson(lesson, offset_days), existing["id"]),
            )
            touched += 1

    conn.execute(
        """
        DELETE FROM exercise_tasks
        WHERE source_lesson_code NOT IN (
            SELECT lesson_code FROM lessons
            WHERE track_code = 'FO' AND lesson_type = 'lesson'
        )
        """
    )
    return touched


class ExerciseSyncOptimizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="exercise-sync-")
        self.db_path = Path(self.tempdir.name) / "sync.db"
        with connect_db(self.db_path) as conn:
            init_db(conn)
            conn.commit()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def insert_lesson(
        self,
        conn: sqlite3.Connection,
        code: str,
        *,
        seen: int = 0,
        cut: int = 0,
        lesson_type: str = "lesson",
        slot_index: int = 1,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lessons (
                slot_key, lesson_code, track_code, lesson_type, title_raw,
                subject_name, subject_prefix, module_label, module_number,
                lesson_number, week_number, day_index, day_name, slot_index,
                recommended_date, is_seen, seen_at, is_cut, source_sheet,
                created_at, updated_at
            ) VALUES (?, ?, 'FO', ?, ?, 'Matemática', 'MAT', 'Módulo 1', 1,
                      ?, 1, 1, 'Segunda', ?, '2026-07-06', ?, ?, ?, 'FO',
                      '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """,
            (
                f"W01-D1-S{slot_index}",
                code,
                lesson_type,
                code,
                slot_index,
                slot_index,
                seen,
                "2026-07-06 10:00:00" if seen else None,
                cut,
            ),
        )

    def insert_task(
        self,
        conn: sqlite3.Connection,
        code: str,
        *,
        status: str = "pending",
        active: int = 0,
        manually_moved: int = 0,
        scheduled_date: str = "2026-08-01",
    ) -> None:
        conn.execute(
            """
            INSERT INTO exercise_tasks (
                source_lesson_code, scheduled_date, status, is_active,
                manually_moved, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '2026-01-02 00:00:00', '2026-01-03 00:00:00')
            """,
            (code, scheduled_date, status, active, manually_moved),
        )

    def task_rows(self, conn: sqlite3.Connection) -> list[tuple[object, ...]]:
        return conn.execute(
            "SELECT * FROM exercise_tasks ORDER BY id"
        ).fetchall()

    def clone_database(self, source: Path, destination: Path) -> None:
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()

    def test_first_sync_and_second_sync_are_idempotent_without_extra_writes(self) -> None:
        with connect_db(self.db_path) as conn:
            self.insert_lesson(conn, "MAT1A1", seen=0, slot_index=1)
            self.insert_lesson(conn, "MAT1A2", seen=1, slot_index=2)
            conn.commit()

            self.assertEqual(sync_fo_exercise_tasks(conn), 2)
            conn.commit()
            first_rows = self.task_rows(conn)
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            self.assertEqual(sync_fo_exercise_tasks(conn), 0)
            conn.set_trace_callback(None)
            second_rows = self.task_rows(conn)

        self.assertEqual(second_rows, first_rows)
        write_statements = [
            sql for sql in statements
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(write_statements, [])

    def test_all_state_transitions_preserve_status_dates_cuts_and_manual_moves(self) -> None:
        with connect_db(self.db_path) as conn:
            self.insert_lesson(conn, "ACTIVE_UNSEEN", seen=0, slot_index=1)
            self.insert_task(conn, "ACTIVE_UNSEEN", active=1, scheduled_date="2026-08-01")
            self.insert_lesson(conn, "SEEN_INACTIVE", seen=1, slot_index=2)
            self.insert_task(conn, "SEEN_INACTIVE", active=0, scheduled_date="2026-08-02")
            self.insert_lesson(conn, "CUT_DONE", seen=1, cut=1, slot_index=3)
            self.insert_task(conn, "CUT_DONE", status="done", active=1, scheduled_date="2026-08-03")
            self.insert_lesson(conn, "SKIPPED", seen=1, slot_index=4)
            self.insert_task(conn, "SKIPPED", status="skipped", active=1, scheduled_date="2026-08-04")
            self.insert_lesson(conn, "MOVED", seen=1, slot_index=5)
            self.insert_task(conn, "MOVED", active=1, manually_moved=1, scheduled_date="2026-12-31")
            self.insert_lesson(conn, "NEW", seen=0, slot_index=6)
            self.insert_lesson(conn, "INACTIVE", lesson_type="list", slot_index=7)
            self.insert_task(conn, "INACTIVE", active=1, scheduled_date="2026-08-07")
            conn.commit()

            touched = sync_fo_exercise_tasks(conn)
            conn.commit()
            rows = {
                row["source_lesson_code"]: dict(row)
                for row in conn.execute("SELECT * FROM exercise_tasks")
            }

        self.assertEqual(touched, 3)
        self.assertEqual(rows["ACTIVE_UNSEEN"]["is_active"], 0)
        self.assertEqual(rows["ACTIVE_UNSEEN"]["scheduled_date"], "2026-08-01")
        self.assertEqual(rows["SEEN_INACTIVE"]["is_active"], 1)
        self.assertEqual(rows["SEEN_INACTIVE"]["scheduled_date"], "2026-07-09")
        self.assertEqual(rows["CUT_DONE"]["status"], "done")
        self.assertEqual(rows["CUT_DONE"]["scheduled_date"], "2026-08-03")
        self.assertEqual(rows["SKIPPED"]["status"], "skipped")
        self.assertEqual(rows["SKIPPED"]["scheduled_date"], "2026-08-04")
        self.assertEqual(rows["MOVED"]["manually_moved"], 1)
        self.assertEqual(rows["MOVED"]["scheduled_date"], "2026-12-31")
        self.assertEqual(rows["NEW"]["status"], "pending")
        self.assertEqual(rows["NEW"]["is_active"], 0)
        self.assertNotIn("INACTIVE", rows)

    def test_legacy_and_optimized_implementations_produce_exact_same_rows(self) -> None:
        with connect_db(self.db_path) as conn:
            self.insert_lesson(conn, "UNSEEN", seen=0, slot_index=1)
            self.insert_task(conn, "UNSEEN", active=1)
            self.insert_lesson(conn, "SEEN", seen=1, slot_index=2)
            self.insert_task(conn, "SEEN", status="done", active=0, manually_moved=1)
            self.insert_lesson(conn, "CUT", seen=1, cut=1, slot_index=3)
            self.insert_task(conn, "CUT", status="skipped", active=1, manually_moved=1)
            self.insert_lesson(conn, "NEW", seen=1, slot_index=4)
            self.insert_lesson(conn, "STALE", lesson_type="list", slot_index=5)
            self.insert_task(conn, "STALE", active=1)
            conn.commit()

        old_path = Path(self.tempdir.name) / "old.db"
        new_path = Path(self.tempdir.name) / "new.db"
        self.clone_database(self.db_path, old_path)
        self.clone_database(self.db_path, new_path)

        while datetime.now().microsecond > 250_000:
            time.sleep(0.01)
        with connect_db(old_path) as old_conn, connect_db(new_path) as new_conn:
            old_touched = legacy_sync_fo_exercise_tasks(old_conn)
            new_touched = sync_fo_exercise_tasks(new_conn)
            old_conn.commit()
            new_conn.commit()
            old_rows = self.task_rows(old_conn)
            new_rows = self.task_rows(new_conn)

        self.assertEqual(new_touched, old_touched)
        self.assertEqual(new_rows, old_rows)

    def test_select_query_count_drops_from_n_plus_one_to_three(self) -> None:
        with connect_db(self.db_path) as conn:
            for index in range(1, 21):
                code = f"MAT1A{index}"
                self.insert_lesson(conn, code, seen=index % 2, slot_index=index)
            conn.commit()
            sync_fo_exercise_tasks(conn)
            conn.commit()

        old_path = Path(self.tempdir.name) / "query-old.db"
        new_path = Path(self.tempdir.name) / "query-new.db"
        self.clone_database(self.db_path, old_path)
        self.clone_database(self.db_path, new_path)

        def select_count(path: Path, sync_function) -> tuple[int, int]:
            statements: list[str] = []
            with connect_db(path) as conn:
                conn.set_trace_callback(statements.append)
                sync_function(conn)
                conn.set_trace_callback(None)
            selects = sum(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            writes = sum(
                sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for sql in statements
            )
            return selects, writes

        self.assertEqual(select_count(old_path, legacy_sync_fo_exercise_tasks), (22, 1))
        self.assertEqual(select_count(new_path, sync_fo_exercise_tasks), (3, 0))

    def test_home_smoke_returns_http_200(self) -> None:
        with connect_db(self.db_path) as conn:
            self.insert_lesson(conn, "MAT1A1", seen=1)
            conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        response = app.test_client().get("/?date=2026-07-10&fo_view=exercicios")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
