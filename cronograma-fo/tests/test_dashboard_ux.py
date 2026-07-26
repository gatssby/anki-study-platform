from __future__ import annotations

import html
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.db import connect_db, init_db
from app.web import create_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DashboardUxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="dashboard-ux-")
        self.db_path = Path(self.tempdir.name) / "dashboard.db"
        with connect_db(self.db_path) as conn:
            init_db(conn)
            for index, status in enumerate(("pending", "done", "skipped"), start=1):
                code = f"MAT1A{index}"
                conn.execute(
                    """
                    INSERT INTO lessons (
                        slot_key, lesson_code, track_code, lesson_type, title_raw,
                        duration_seconds, subject_name, subject_prefix, module_label,
                        module_number, lesson_number, week_number, day_index, day_name,
                        slot_index, recommended_date, is_seen, seen_at, is_cut, source_sheet
                    ) VALUES (?, ?, 'FO', 'lesson', ?, 1800, 'Matemática', 'MAT',
                              'Módulo 1', 1, ?, 1, 5, 'Sexta', ?, '2026-07-10',
                              1, '2026-07-10 08:00:00', 0, 'FO')
                    """,
                    (f"W01-D5-S{index}", code, f"Aula {index}", index, index),
                )
                conn.execute(
                    """
                    INSERT INTO exercise_tasks (
                        source_lesson_code, scheduled_date, status, is_active, manually_moved
                    ) VALUES (?, '2026-07-10', ?, 1, 1)
                    """,
                    (code, status),
                )
            conn.execute(
                """
                INSERT INTO lessons (
                    slot_key, lesson_code, track_code, lesson_type, title_raw,
                    relative_path, duration_seconds, subject_name, subject_prefix,
                    module_label, module_number, lesson_number, week_number, day_index,
                    day_name, slot_index, recommended_date, is_seen, is_cut, source_sheet
                ) VALUES (
                    'UN-S1', 'UN-MAT-1', 'UN', 'lesson', 'Aula UN',
                    'UN/Matemática/Módulo 1/Aula UN', 1200, 'Matemática', 'MAT',
                    'Módulo 1', 1, 1, 1, 5, 'Sexta', 1, '2026-07-10', 0, 0, 'UN'
                )
                """
            )
            conn.commit()
            self.task_id = conn.execute(
                "SELECT id FROM exercise_tasks WHERE source_lesson_code='MAT1A1'"
            ).fetchone()[0]
        self.app = create_app(self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_home_smoke_and_query_context_for_every_fo_view(self) -> None:
        for fo_view in ("aulas", "exercicios", "tudo"):
            response = self.client.get(
                f"/?date=2026-07-10&fo_view={fo_view}&context=keep&context=two"
            )
            self.assertEqual(response.status_code, 200)
            page = html.unescape(response.get_data(as_text=True))
            expected = f"/?date=2026-07-10&fo_view={fo_view}&context=keep&context=two"
            self.assertIn(f'value="{expected}"', page)
            self.assertIn("Cronograma", page)
            self.assertIn('aria-current="page"', page)
            self.assertIn('class="app-skip-link"', page)

    def test_fo_action_preserves_each_view_and_query_string(self) -> None:
        for fo_view in ("aulas", "exercicios", "tudo"):
            return_to = f"/?date=2026-07-10&fo_view={fo_view}&context=keep"
            response = self.client.post(
                "/lessons/MAT1A1/toggle-seen",
                data={
                    "next_date": "2026-07-10",
                    "fo_view": fo_view,
                    "return_to": return_to,
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], return_to)

    def test_un_and_exercise_actions_preserve_full_return_url(self) -> None:
        for fo_view in ("aulas", "exercicios", "tudo"):
            return_to = f"/?date=2026-07-10&fo_view={fo_view}&context=keep"
            response = self.client.post(
                "/lessons/UN-MAT-1/toggle-seen",
                data={
                    "next_date": "2026-07-10",
                    "fo_view": fo_view,
                    "return_to": return_to,
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], return_to)

        return_to = "/?date=2026-07-10&fo_view=tudo&context=keep"
        actions = (
            (
                "/un/modules/mark-seen",
                {"next_date": "2026-07-10", "module_path": "UN/Matemática/Módulo 1"},
            ),
            (
                f"/exercise-tasks/{self.task_id}/status",
                {"next_date": "2026-07-10", "status": "done"},
            ),
            (
                f"/exercise-tasks/{self.task_id}/reschedule",
                {"next_date": "2026-07-10", "scheduled_date": "2026-07-11"},
            ),
        )
        for path, payload in actions:
            response = self.client.post(
                path,
                data={**payload, "fo_view": "tudo", "return_to": return_to},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], return_to)

    def test_external_return_is_blocked_and_safe_fallback_keeps_view(self) -> None:
        response = self.client.post(
            "/lessons/MISSING/toggle-seen",
            data={
                "next_date": "2026-07-10",
                "fo_view": "tudo",
                "return_to": "https://example.invalid/escape",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/?date=2026-07-10&fo_view=tudo")

    def test_exercise_statuses_are_translated_without_changing_values(self) -> None:
        response = self.client.get("/?date=2026-07-10&fo_view=exercicios")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('class="app-status app-status--pending"', page)
        self.assertIn("<span>Pendente</span>", page)
        self.assertIn('class="app-status app-status--done"', page)
        self.assertIn("<span>Concluído</span>", page)
        self.assertIn('class="app-status app-status--skipped"', page)
        self.assertIn("<span>Ignorado</span>", page)
        self.assertIn('name="status" value="pending"', page)
        self.assertIn('name="status" value="done"', page)
        self.assertIn('name="status" value="skipped"', page)

    def test_relevant_forms_opt_into_scroll_and_un_module_preservation(self) -> None:
        response = self.client.get("/?date=2026-07-10&fo_view=tudo&context=keep")
        page = response.get_data(as_text=True)
        self.assertGreaterEqual(page.count("data-preserve-scroll"), 8)
        self.assertGreaterEqual(page.count("data-preserve-un-modules"), 2)

    def test_visual_foundation_is_local_ordered_and_table_free(self) -> None:
        response = self.client.get("/?date=2026-07-10&fo_view=tudo")
        page = response.get_data(as_text=True)

        tabler_index = page.index("/static/vendor/tabler/tabler.min.css")
        tokens_index = page.index("/static/css/tokens.css")
        app_index = page.index("/static/css/app.css")
        dashboard_index = page.index("/static/css/dashboard.css")
        responsive_index = page.index("/static/css/responsive.css")
        self.assertLess(tabler_index, tokens_index)
        self.assertLess(tokens_index, app_index)
        self.assertLess(app_index, dashboard_index)
        self.assertLess(dashboard_index, responsive_index)
        self.assertNotIn("/static/style.css", page)
        self.assertNotIn("fonts.googleapis.com", page)
        self.assertNotIn("<table", page)

    def test_dashboard_keeps_date_and_un_javascript_contracts(self) -> None:
        response = self.client.get("/?date=2026-07-10&fo_view=tudo")
        page = response.get_data(as_text=True)

        for contract in (
            "data-date-form",
            "data-date-display",
            "data-date-picker",
            "data-date-iso",
            "data-date-error",
            "data-un-modules",
            "data-un-module-details",
            "data-un-module-id",
            "data-preserve-scroll",
            "data-preserve-un-modules",
        ):
            self.assertIn(contract, page)


class DashboardJavascriptStateTest(unittest.TestCase):
    def test_scroll_and_module_state_in_node(self) -> None:
        result = subprocess.run(
            ["node", str(PROJECT_ROOT / "tests" / "dashboard_scroll_state_test.js")],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dashboard_scroll_state_test=ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
