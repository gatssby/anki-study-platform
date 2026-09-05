from __future__ import annotations

import os
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
          compare_db_state_before_upload() {{ echo compare-db >> "$CALLS_FILE"; }}
          confirm_db_deploy() {{ echo confirm-db >> "$CALLS_FILE"; }}
          deploy_db_candidate() {{ echo deploy-db >> "$CALLS_FILE"; }}
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

    def test_with_db_keeps_code_rsync_separate_from_guarded_database_upload(self) -> None:
        valid_db = self.root / "valid.db"
        create_sqlite(valid_db, "candidate")

        completed = self.run_main("--with-db", local_db=valid_db)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.recorded_calls(),
            [
                "compile",
                "remote-check",
                "ensure-backup-dir",
                "rsync",
                "backup",
                "compare-db",
                "confirm-db",
                "deploy-db",
            ],
        )

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


class CronogramaDeployRsyncPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="cronograma-rsync-policy-")
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.args_file = self.root / "rsync-args.txt"
        self.source.mkdir()
        self.destination.mkdir()

        for relative, content in {
            "app/main.py": "print('new code')\n",
            "scripts/task.sh": "#!/usr/bin/env bash\n",
            "tests/test_app.py": "def test_placeholder(): pass\n",
            "docker-compose.yml": "services: {}\n",
            "requirements.txt": "Flask==3.1.1\n",
            "data/cronograma.db": "local database",
            "data/secondary.db": "local secondary database",
            "data/cache.sqlite": "local sqlite",
            "data/cache.sqlite3": "local sqlite3",
            "data/review_questions/uploads/local.png": "local upload",
            "app/embedded.db": "local embedded database",
        }.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        for relative, content in {
            "data/cronograma.db": "remote database",
            "data/secondary.db": "remote secondary database",
            "data/cache.sqlite": "remote sqlite",
            "data/cache.sqlite3": "remote sqlite3",
            "data/review_questions/question.json": "remote question",
            "data/review_questions/uploads/remote.png": "remote upload",
            "data/backups/snapshot.db": "remote backup",
            "app/remote-only.db": "remote embedded database",
            "obsolete.txt": "delete me outside protected data",
        }.items():
            path = self.destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        self.persistent_directories = [
            self.destination / "data",
            self.destination / "data/review_questions",
            self.destination / "data/review_questions/uploads",
        ]
        fixed_time_ns = 1_600_000_000_000_000_000
        for directory in reversed(self.persistent_directories):
            os.utime(directory, ns=(fixed_time_ns, fixed_time_ns))
        self.persistent_mtimes = {
            directory: directory.stat().st_mtime_ns
            for directory in self.persistent_directories
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_rsync_project(
        self, *, dry_run: bool = False, inject_delete: bool = False
    ) -> subprocess.CompletedProcess[str]:
        shell = f"""
          export CRONOGRAMA_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(DEPLOY_SCRIPT))}
          PROJECT_DIR={shlex.quote(str(self.source))}
          REMOTE_HOST=fixture
          REMOTE_DIR=/fixture
          DRY_RUN={1 if dry_run else 0}
          DESTINATION={shlex.quote(str(self.destination))}
          ARGS_FILE={shlex.quote(str(self.args_file))}
          INJECT_DELETE={1 if inject_delete else 0}
          rsync() {{
            printf '%s\n' "$@" > "$ARGS_FILE"
            local args=("$@")
            local destination_index=$((${{#args[@]}} - 1))
            local source_index=$((destination_index - 1))
            local source_path="${{args[$source_index]}}"
            local prefix=("${{args[@]:0:$source_index}}")
            if [ "$INJECT_DELETE" -eq 1 ]; then
              command rsync "${{prefix[@]}}" --delete "$source_path" "$DESTINATION/"
            else
              command rsync "${{prefix[@]}}" "$source_path" "$DESTINATION/"
            fi
          }}
          rsync_project
        """
        return subprocess.run(
            ["bash", "-c", shell],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_persistent_data_unchanged(self) -> None:
        expected = {
            "data/cronograma.db": "remote database",
            "data/secondary.db": "remote secondary database",
            "data/cache.sqlite": "remote sqlite",
            "data/cache.sqlite3": "remote sqlite3",
            "data/review_questions/question.json": "remote question",
            "data/review_questions/uploads/remote.png": "remote upload",
            "data/backups/snapshot.db": "remote backup",
        }
        for relative, content in expected.items():
            self.assertEqual(
                (self.destination / relative).read_text(encoding="utf-8"), content
            )
        self.assertFalse(
            (self.destination / "data/review_questions/uploads/local.png").exists()
        )
        for directory, original_mtime in self.persistent_mtimes.items():
            self.assertEqual(directory.stat().st_mtime_ns, original_mtime)

    def test_normal_rsync_excludes_and_receiver_protects_all_data(self) -> None:
        completed = self.run_rsync_project(inject_delete=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        args = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--filter", args)
        self.assertIn("protect /data/***", args)
        self.assertIn("--exclude", args)
        self.assertIn("/data/", args)
        self.assertNotIn("--delete", args)
        self.assertNotIn("--delete-excluded", args)
        self.assert_persistent_data_unchanged()

        self.assertEqual(
            (self.destination / "app/main.py").read_text(encoding="utf-8"),
            "print('new code')\n",
        )
        self.assertTrue((self.destination / "scripts/task.sh").exists())
        self.assertTrue((self.destination / "tests/test_app.py").exists())
        self.assertTrue((self.destination / "docker-compose.yml").exists())
        self.assertTrue((self.destination / "requirements.txt").exists())
        self.assertFalse((self.destination / "app/embedded.db").exists())
        self.assertEqual(
            (self.destination / "app/remote-only.db").read_text(encoding="utf-8"),
            "remote embedded database",
        )
        self.assertFalse((self.destination / "obsolete.txt").exists())

    def test_dry_run_uses_identical_data_protection_without_writes(self) -> None:
        completed = self.run_rsync_project(dry_run=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        args = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--dry-run", args)
        self.assertIn("protect /data/***", args)
        self.assertIn("/data/", args)
        self.assertFalse((self.destination / "app/main.py").exists())
        self.assert_persistent_data_unchanged()


if __name__ == "__main__":
    unittest.main()
