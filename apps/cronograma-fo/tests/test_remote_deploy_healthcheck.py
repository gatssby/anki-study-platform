from __future__ import annotations

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "remote_deploy_live.sh"


class RemoteDeployHealthcheckTest(unittest.TestCase):
    def run_scenario(self, body: str) -> subprocess.CompletedProcess[str]:
        shell = f"""
          export REMOTE_DEPLOY_LIB_ONLY=1
          source {shlex.quote(str(REMOTE_DEPLOY_SCRIPT))}
          docker() {{ :; }}
          curl() {{ :; }}
          sleep() {{ :; }}
          show_recent_container_logs() {{ echo logs_shown >&2; }}
          HEALTHCHECK_INTERVAL_SECONDS=0
          HEALTHCHECK_HTTP_TIMEOUT_SECONDS=1
          {body}
        """
        return subprocess.run(
            ["bash", "-c", shell],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_healthy_app_passes_on_first_attempt(self) -> None:
        result = self.run_scenario(
            """
            HEALTHCHECK_MAX_ATTEMPTS=3
            container_is_running() { return 0; }
            app_process_is_running() { return 0; }
            http_status() { printf 200; }
            healthcheck_app
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Healthcheck aprovado na tentativa 1", result.stdout)
        self.assertNotIn("logs_shown", result.stderr)

    def test_container_that_never_starts_fails_and_shows_logs(self) -> None:
        result = self.run_scenario(
            """
            HEALTHCHECK_MAX_ATTEMPTS=2
            container_is_running() { return 1; }
            app_process_is_running() { return 0; }
            http_status() { printf 200; }
            healthcheck_app
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("container não está em execução", result.stderr)
        self.assertIn("logs_shown", result.stderr)

    def test_running_container_with_failed_http_is_rejected(self) -> None:
        result = self.run_scenario(
            """
            HEALTHCHECK_MAX_ATTEMPTS=1
            container_is_running() { return 0; }
            app_process_is_running() { return 0; }
            http_status() { printf 000; }
            healthcheck_app
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP retornou 000", result.stderr)
        self.assertIn("logs_shown", result.stderr)

    def test_http_that_becomes_ready_after_retries_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="healthcheck-retry-") as root:
            counter = Path(root) / "attempts"
            counter.write_text("0", encoding="utf-8")
            result = self.run_scenario(
                f"""
                HEALTHCHECK_MAX_ATTEMPTS=5
                container_is_running() {{ return 0; }}
                app_process_is_running() {{ return 0; }}
                http_status() {{
                  count=$(<{shlex.quote(str(counter))})
                  count=$((count + 1))
                  echo "$count" > {shlex.quote(str(counter))}
                  if [ "$count" -lt 3 ]; then printf 503; else printf 200; fi
                }}
                healthcheck_app
                """
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text(encoding="utf-8").strip(), "3")
            self.assertIn("Healthcheck aprovado na tentativa 3", result.stdout)

    def test_final_timeout_returns_error_after_limited_attempts(self) -> None:
        result = self.run_scenario(
            """
            HEALTHCHECK_MAX_ATTEMPTS=3
            container_is_running() { return 0; }
            app_process_is_running() { return 0; }
            http_status() { printf 503; }
            healthcheck_app
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count("healthcheck tentativa"), 3)
        self.assertIn("falhou após 3 tentativas", result.stderr)


if __name__ == "__main__":
    unittest.main()
