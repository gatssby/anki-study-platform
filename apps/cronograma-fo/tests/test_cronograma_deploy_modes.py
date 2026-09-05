from __future__ import annotations

import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = PROJECT_ROOT / "cronograma_deploy.sh"


def create_sqlite(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE state (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


class CronogramaDeployModesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.missing_db = self.root / "missing" / "cronograma.db"
        self.calls = self.root / "calls.txt"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_main(self, *args: str, local_db: Path | None = None) -> subprocess.CompletedProcess[str]:
        database = local_db or self.missing_db
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          LOCAL_DB={shlex.quote(str(database))}
          CALLS_FILE={shlex.quote(str(self.calls))}
          require_command() {{ :; }}
          compile_python_files() {{ echo compile >> "$CALLS_FILE"; }}
          check_remote_readonly() {{ echo remote-check >> "$CALLS_FILE"; }}
          ensure_remote_backup_dir() {{ echo ensure-backup-dir >> "$CALLS_FILE"; }}
          rsync_project() {{ echo rsync >> "$CALLS_FILE"; }}
          backup_remote_db() {{ echo backup >> "$CALLS_FILE"; BACKUP_CREATED=1; }}
          run_remote_script() {{ echo run-remote >> "$CALLS_FILE"; }}
          run_restart_only() {{ echo restart-only >> "$CALLS_FILE"; }}
          pull_remote_db() {{ echo pull-db >> "$CALLS_FILE"; }}
          main "$@"
        """
        return subprocess.run(
            ["bash", "-c", shell, "cronograma-deploy-test", *args],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def recorded_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()

    def test_normal_deploy_allows_missing_local_database(self) -> None:
        completed = self.run_main()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Banco local:   ausente (OK para deploy sem DB)", completed.stdout)
        self.assertEqual(
            self.recorded_calls(),
            ["compile", "remote-check", "ensure-backup-dir", "rsync", "backup", "run-remote"],
        )

    def test_dry_run_allows_missing_local_database(self) -> None:
        completed = self.run_main("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_calls(), ["compile", "remote-check", "rsync"])

    def test_restart_only_allows_missing_local_database(self) -> None:
        completed = self.run_main("--restart-only")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_calls(), ["compile", "restart-only"])

    def test_pull_db_allows_missing_local_database(self) -> None:
        completed = self.run_main("--pull-db")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_calls(), ["remote-check", "pull-db"])

    def test_with_db_rejects_missing_local_database_before_upload(self) -> None:
        completed = self.run_main("--with-db")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--with-db exige banco local", completed.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_with_db_rejects_invalid_local_database_before_upload(self) -> None:
        invalid_db = self.root / "invalid.db"
        invalid_db.write_text("not sqlite", encoding="utf-8")

        completed = self.run_main("--with-db", local_db=invalid_db)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--with-db exige banco local SQLite válido", completed.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_pull_db_without_local_database_downloads_without_local_backup(self) -> None:
        remote_db = self.root / "remote.db"
        create_sqlite(remote_db, "remote")
        local_db = self.root / "new-local" / "cronograma.db"
        project_dir = self.root / "relocated-app"
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          PROJECT_DIR={shlex.quote(str(project_dir))}
          LOCAL_DB={shlex.quote(str(local_db))}
          REMOTE_FIXTURE={shlex.quote(str(remote_db))}
          backup_remote_db() {{ REMOTE_DB_SNAPSHOT="$REMOTE_FIXTURE"; }}
          rsync() {{ cp "$REMOTE_DB_SNAPSHOT" "${{@: -1}}"; }}
          collect_local_db_stats() {{ :; }}
          pull_remote_db
        """

        completed = subprocess.run(
            ["bash", "-c", shell], check=False, capture_output=True, text=True
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("nenhum backup local necessário", completed.stdout)
        self.assertFalse((project_dir / "data" / "backups" / "manual").exists())
        connection = sqlite3.connect(local_db)
        try:
            marker = connection.execute("SELECT marker FROM state").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(marker, "remote")

    def test_pull_db_with_local_database_preserves_backup_before_replacement(self) -> None:
        remote_db = self.root / "remote.db"
        local_db = self.root / "existing" / "cronograma.db"
        create_sqlite(remote_db, "remote")
        create_sqlite(local_db, "local")
        project_dir = self.root / "relocated-app"
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          PROJECT_DIR={shlex.quote(str(project_dir))}
          LOCAL_DB={shlex.quote(str(local_db))}
          REMOTE_FIXTURE={shlex.quote(str(remote_db))}
          backup_remote_db() {{ REMOTE_DB_SNAPSHOT="$REMOTE_FIXTURE"; }}
          rsync() {{ cp "$REMOTE_DB_SNAPSHOT" "${{@: -1}}"; }}
          collect_local_db_stats() {{ :; }}
          pull_remote_db
        """

        completed = subprocess.run(
            ["bash", "-c", shell], check=False, capture_output=True, text=True
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        backups = list((project_dir / "data" / "backups" / "manual").glob("*.db"))
        self.assertEqual(len(backups), 1)
        backup_connection = sqlite3.connect(backups[0])
        local_connection = sqlite3.connect(local_db)
        try:
            backup_marker = backup_connection.execute("SELECT marker FROM state").fetchone()[0]
            local_marker = local_connection.execute("SELECT marker FROM state").fetchone()[0]
        finally:
            backup_connection.close()
            local_connection.close()
        self.assertEqual(backup_marker, "local")
        self.assertEqual(local_marker, "remote")


if __name__ == "__main__":
    unittest.main()
