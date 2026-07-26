from __future__ import annotations

import hashlib
import shlex
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.compare_sqlite_state import compare_database_state
from scripts.sqlite_safe_backup import (
    assert_exclusive_access,
    create_sqlite_snapshot,
    restore_sqlite_snapshot,
    validate_sqlite_database,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = PROJECT_ROOT / "cronograma_deploy.sh"
BACKUP_TOOL = PROJECT_ROOT / "scripts" / "sqlite_safe_backup.py"

SCHEMA = """
CREATE TABLE lessons (
    slot_key TEXT PRIMARY KEY,
    lesson_code TEXT NOT NULL UNIQUE,
    track_code TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    is_seen INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT,
    is_cut INTEGER NOT NULL DEFAULT 0,
    cut_reason TEXT,
    cut_source TEXT
);
CREATE TABLE exercise_tasks (
    id INTEGER PRIMARY KEY,
    source_lesson_code TEXT NOT NULL UNIQUE,
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL,
    is_active INTEGER NOT NULL,
    manually_moved INTEGER NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE daily_assignments (
    dashboard_date TEXT NOT NULL,
    planned_slot_key TEXT NOT NULL,
    assigned_lesson_code TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (dashboard_date, planned_slot_key)
);
CREATE TABLE un_daily_assignments (
    dashboard_date TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    assigned_lesson_code TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (dashboard_date, row_index)
);
CREATE TABLE schedule_settings (id INTEGER PRIMARY KEY, exam_date TEXT, updated_at TEXT);
CREATE TABLE schedule_unavailability (
    id INTEGER PRIMARY KEY, start_date TEXT, end_date TEXT, capacity_percent INTEGER, reason TEXT
);
CREATE TABLE app_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_at TEXT);
CREATE TABLE review_questions (
    id INTEGER PRIMARY KEY,
    question_image_path TEXT,
    question_image_paths TEXT,
    answer_image_path TEXT,
    review_count INTEGER
);
CREATE TABLE review_question_attempts (
    id INTEGER PRIMARY KEY, question_id INTEGER, reviewed_at TEXT, result TEXT
);
"""


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_state_db(path: Path, *, marker: str = "base") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO lessons VALUES (?, ?, 'FO', 'lesson', ?, ?, ?, ?, ?)",
            [
                ("W01-D1-S1", "FO-1", 1, "2026-07-01T10:00:00", 0, None, None),
                ("W01-D1-S2", "FO-2", 0, None, 1, "manual", "manual"),
            ],
        )
        conn.execute(
            "INSERT INTO exercise_tasks VALUES (1, 'FO-1', '2026-07-10', 'done', 0, 1, 'c', 'u')"
        )
        conn.execute(
            "INSERT INTO daily_assignments VALUES ('2026-07-10', 'W01-D1-S1', 'FO-1', 'c', 'u')"
        )
        conn.execute(
            "INSERT INTO un_daily_assignments VALUES ('2026-07-10', 1, 'FO-2', 'c', 'u')"
        )
        conn.execute("INSERT INTO schedule_settings VALUES (1, '2026-12-01', 'u')")
        conn.execute(
            "INSERT INTO schedule_unavailability VALUES (1, '2026-07-20', '2026-07-21', 0, 'ferias')"
        )
        conn.execute("INSERT INTO app_settings VALUES ('marker', ?, 'u')", (marker,))
        conn.execute("INSERT INTO review_questions VALUES (1, NULL, '[]', NULL, 2)")
        conn.execute(
            "INSERT INTO review_question_attempts VALUES (1, 1, '2026-07-09T10:00:00', 'correct')"
        )
        conn.commit()
    finally:
        conn.close()


class SQLiteOperationalSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.uploads = self.root / "data"
        self.uploads.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def clone(self, source: Path, name: str) -> Path:
        destination = self.root / name
        create_sqlite_snapshot(source, destination)
        return destination

    def compare(self, baseline: Path, candidate: Path):
        return compare_database_state(baseline, candidate, uploads_root=self.uploads)

    def test_backup_during_concurrent_writes_is_a_consistent_point_in_time(self) -> None:
        source = self.root / "active.db"
        conn = sqlite3.connect(source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.commit()
        conn.close()
        started = threading.Event()

        def write_rows() -> None:
            writer = sqlite3.connect(source)
            try:
                for value in range(1, 81):
                    writer.execute("INSERT INTO events VALUES (?, ?)", (value, str(value)))
                    writer.commit()
                    started.set()
                    time.sleep(0.001)
            finally:
                writer.close()

        thread = threading.Thread(target=write_rows)
        thread.start()
        self.assertTrue(started.wait(2))
        snapshot = self.root / "concurrent-snapshot.db"
        create_sqlite_snapshot(source, snapshot)
        thread.join(5)
        self.assertFalse(thread.is_alive())

        snap_conn = sqlite3.connect(snapshot)
        count, minimum, maximum = snap_conn.execute(
            "SELECT COUNT(*), MIN(id), MAX(id) FROM events"
        ).fetchone()
        snap_conn.close()
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(count, 80)
        self.assertEqual((minimum, maximum), (1, count))
        self.assertEqual(validate_sqlite_database(snapshot).integrity_check, "ok")

    def test_snapshot_is_integral_and_preserves_all_committed_rows(self) -> None:
        source = self.root / "source.db"
        create_state_db(source)
        snapshot = self.clone(source, "snapshot.db")

        source_conn = sqlite3.connect(source)
        snapshot_conn = sqlite3.connect(snapshot)
        source_dump = "\n".join(source_conn.iterdump())
        snapshot_dump = "\n".join(snapshot_conn.iterdump())
        source_conn.close()
        snapshot_conn.close()

        self.assertEqual(snapshot_dump, source_dump)
        self.assertTrue(validate_sqlite_database(snapshot).is_valid)

    def test_identical_candidate_is_safe(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        self.assertTrue(self.compare(baseline, candidate).safe)

    def test_candidate_with_fewer_seen_lessons_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute("UPDATE lessons SET is_seen=0, seen_at=NULL WHERE lesson_code='FO-1'")
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        self.assertIn("seen_lessons", {item.category for item in report.differences})

    def test_seen_and_cut_identity_changes_are_rejected_even_with_equal_totals(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute("UPDATE lessons SET is_seen=0, seen_at=NULL, is_cut=1 WHERE lesson_code='FO-1'")
        conn.execute("UPDATE lessons SET is_seen=1, seen_at='other', is_cut=0 WHERE lesson_code='FO-2'")
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        categories = {item.category for item in report.differences}
        self.assertIn("seen_lessons", categories)
        self.assertIn("cuts", categories)

    def test_candidate_with_regressed_exercise_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute(
            "UPDATE exercise_tasks SET status='pending', manually_moved=0 WHERE source_lesson_code='FO-1'"
        )
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        self.assertIn("exercise_tasks", {item.category for item in report.differences})

    def test_candidate_missing_existing_assignment_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute("DELETE FROM daily_assignments")
        conn.execute("DELETE FROM un_daily_assignments")
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        categories = {item.category for item in report.differences}
        self.assertIn("daily_assignments", categories)
        self.assertIn("un_daily_assignments", categories)

    def test_candidate_with_different_configuration_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute("UPDATE schedule_settings SET exam_date='2027-01-01'")
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        self.assertIn("schedule_settings", {item.category for item in report.differences})

    def test_missing_referenced_upload_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute(
            "UPDATE review_questions SET question_image_path='review_questions/uploads/missing.png'"
        )
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        self.assertIn("review_questions/uploads/missing.png", report.missing_uploads)

    def test_review_question_or_attempt_regression_is_rejected(self) -> None:
        baseline = self.root / "baseline.db"
        create_state_db(baseline)
        candidate = self.clone(baseline, "candidate.db")
        conn = sqlite3.connect(candidate)
        conn.execute("UPDATE review_questions SET review_count=1 WHERE id=1")
        conn.execute("DELETE FROM review_question_attempts WHERE id=1")
        conn.commit()
        conn.close()
        report = self.compare(baseline, candidate)
        categories = {item.category for item in report.differences}
        self.assertIn("review_questions", categories)
        self.assertIn("review_question_attempts", categories)

    def test_replacement_requires_exclusive_access_and_succeeds_when_stopped(self) -> None:
        live = self.root / "live.db"
        create_state_db(live, marker="old")
        candidate = self.root / "candidate.db"
        create_state_db(candidate, marker="new")
        writer = sqlite3.connect(live)
        writer.execute("BEGIN IMMEDIATE")
        with self.assertRaises(RuntimeError):
            assert_exclusive_access(live)
        writer.rollback()
        writer.close()

        assert_exclusive_access(live)
        restore_sqlite_snapshot(candidate, live)
        conn = sqlite3.connect(live)
        marker = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key='marker'").fetchone()[0]
        conn.close()
        self.assertEqual(marker, "new")

    def test_failed_boot_after_replacement_restores_snapshot(self) -> None:
        live = self.root / "live.db"
        create_state_db(live, marker="old")
        snapshot = self.clone(live, "rollback.db")
        candidate = self.root / "candidate.db"
        create_state_db(candidate, marker="new")
        counter = self.root / "starts"
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          REMOTE_DB={shlex.quote(str(live))}
          REMOTE_DB_SNAPSHOT={shlex.quote(str(snapshot))}
          REMOTE_DB_CANDIDATE={shlex.quote(str(candidate))}
          stop_remote_app_for_db_replace() {{ python3 {shlex.quote(str(BACKUP_TOOL))} assert-exclusive --db "$REMOTE_DB"; }}
          restore_remote_db_from() {{ python3 {shlex.quote(str(BACKUP_TOOL))} restore --source "$1" --destination "$REMOTE_DB" >/dev/null; }}
          cleanup_remote_candidate() {{ :; }}
          run_remote_script() {{
            local count=0
            [ -f {shlex.quote(str(counter))} ] && count=$(<{shlex.quote(str(counter))})
            count=$((count + 1))
            echo "$count" > {shlex.quote(str(counter))}
            [ "$count" -gt 1 ]
          }}
          validate_remote_app_after_db_replace() {{ return 0; }}
          die() {{ exit 23; }}
          deploy_db_candidate
        """
        completed = subprocess.run(["bash", "-c", shell], check=False)
        self.assertEqual(completed.returncode, 23)
        conn = sqlite3.connect(live)
        marker = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key='marker'").fetchone()[0]
        conn.close()
        self.assertEqual(marker, "old")

    def test_normal_deploy_without_database_does_not_touch_database(self) -> None:
        live = self.root / "live.db"
        create_state_db(live)
        before = (live.stat().st_size, live.stat().st_mtime_ns, file_hash(live))
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          export LOCAL_DB={shlex.quote(str(live))}
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          check_local_prerequisites() {{ :; }}
          compile_python_files() {{ :; }}
          check_remote_readonly() {{ :; }}
          ensure_remote_backup_dir() {{ :; }}
          rsync_project() {{ :; }}
          run_remote_script() {{ :; }}
          backup_remote_db() {{ return 99; }}
          main --no-restart
        """
        subprocess.run(["bash", "-c", shell], check=True)
        after = (live.stat().st_size, live.stat().st_mtime_ns, file_hash(live))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
