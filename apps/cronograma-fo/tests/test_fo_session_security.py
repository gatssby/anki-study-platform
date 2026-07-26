from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fo_session_security import protect_storage_state, save_storage_state


def permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class FakeBrowserContext:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def storage_state(self, *, path: str) -> None:
        Path(path).write_text(json.dumps(self.payload), encoding="utf-8")


class FailingBrowserContext:
    def storage_state(self, *, path: str) -> None:
        Path(path).write_text("partial", encoding="utf-8")
        raise RuntimeError("simulated storage_state failure")


class FoSessionSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="fo-session-security-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_new_storage_state_is_created_with_private_permissions(self) -> None:
        session_file = self.root / "session" / "storage_state.json"
        payload = {"cookies": [{"name": "session", "value": "secret"}]}

        save_storage_state(FakeBrowserContext(payload), session_file)

        self.assertEqual(json.loads(session_file.read_text(encoding="utf-8")), payload)
        self.assertEqual(permissions(session_file), 0o600)
        self.assertEqual(permissions(session_file.parent), 0o700)

    def test_existing_storage_state_is_updated_atomically_and_restricted(self) -> None:
        session_dir = self.root / "session"
        session_dir.mkdir(mode=0o755)
        session_file = session_dir / "storage_state.json"
        session_file.write_text('{"cookies":[{"value":"old"}]}', encoding="utf-8")
        session_file.chmod(0o644)
        payload = {"cookies": [{"value": "new"}], "origins": []}

        save_storage_state(FakeBrowserContext(payload), session_file)

        self.assertEqual(json.loads(session_file.read_text(encoding="utf-8")), payload)
        self.assertEqual(permissions(session_file), 0o600)
        self.assertEqual(permissions(session_dir), 0o700)

    def test_protect_existing_state_changes_only_permissions(self) -> None:
        session_dir = self.root / "session"
        session_dir.mkdir(mode=0o755)
        session_file = session_dir / "storage_state.json"
        original = b'{"cookies":[{"value":"unchanged"}]}'
        session_file.write_bytes(original)
        session_file.chmod(0o644)

        protect_storage_state(session_file)

        self.assertEqual(session_file.read_bytes(), original)
        self.assertEqual(permissions(session_file), 0o600)
        self.assertEqual(permissions(session_dir), 0o700)

    def test_failed_update_preserves_existing_state_and_removes_temporary_file(self) -> None:
        session_file = self.root / "session" / "storage_state.json"
        protect_storage_state(session_file)
        original = b'{"cookies":[{"value":"valid"}]}'
        session_file.write_bytes(original)
        session_file.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            save_storage_state(FailingBrowserContext(), session_file)

        self.assertEqual(session_file.read_bytes(), original)
        self.assertEqual(permissions(session_file), 0o600)
        self.assertEqual(list(session_file.parent.glob(".*.tmp")), [])

    def test_deploy_and_docker_context_exclude_session_credentials(self) -> None:
        deploy = (PROJECT_ROOT / "cronograma_deploy.sh").read_text(encoding="utf-8")
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        self.assertIn('--exclude "state/"', deploy)
        self.assertIn('--exclude "work/"', deploy)
        self.assertIn("state/", dockerignore)
        self.assertIn("work/", dockerignore)
        self.assertIn("work/**", dockerignore)
        self.assertIn("work/fo_bridge/session/", dockerignore)
        self.assertIn("work/**/session/", dockerignore)
        self.assertIn("**/storage_state.json", dockerignore)
        self.assertIn("**/fo_storage_state.json", dockerignore)
        self.assertIn("**/*storage_state*.json", dockerignore)


if __name__ == "__main__":
    unittest.main()
