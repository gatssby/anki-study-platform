from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path

import app.review_questions as review_questions_module
from app.db import connect_db, init_db
from app.web import create_app


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class SecondaryPagesVisualTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="secondary-pages-")
        root = Path(self.tempdir.name)
        self.db_path = root / "test.db"
        self._path_values = {
            "APP_DATA_ROOT": review_questions_module.APP_DATA_ROOT,
            "REVIEW_QUESTION_STATE_DIR": review_questions_module.REVIEW_QUESTION_STATE_DIR,
            "REVIEW_QUESTION_UPLOADS_DIR": review_questions_module.REVIEW_QUESTION_UPLOADS_DIR,
            "LEGACY_REVIEW_QUESTION_UPLOADS_DIR": review_questions_module.LEGACY_REVIEW_QUESTION_UPLOADS_DIR,
        }
        review_questions_module.APP_DATA_ROOT = root
        review_questions_module.REVIEW_QUESTION_STATE_DIR = root / "review_questions"
        review_questions_module.REVIEW_QUESTION_UPLOADS_DIR = root / "review_questions" / "uploads"
        review_questions_module.LEGACY_REVIEW_QUESTION_UPLOADS_DIR = root / "legacy-uploads"

        with connect_db(self.db_path) as conn:
            init_db(conn)
            for code, track, slot in (("FO-1", "FO", "W01-D1-S1"), ("UN-1", "UN", "UN-S1")):
                conn.execute(
                    """
                    INSERT INTO lessons (
                        slot_key, lesson_code, track_code, lesson_type, title_raw,
                        duration_seconds, subject_name, subject_prefix, module_label,
                        week_number, day_index, day_name, slot_index, recommended_date,
                        is_seen, is_cut, source_sheet
                    ) VALUES (?, ?, ?, 'lesson', ?, 1200, 'Biologia', 'BIO',
                              'Módulo 1', 1, 1, 'Segunda', 1, '2026-07-06', 0, 0, ?)
                    """,
                    (slot, code, track, f"Aula {track}", track),
                )
            conn.commit()

        self.app = create_app(self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        for name, value in self._path_values.items():
            setattr(review_questions_module, name, value)
        self.tempdir.cleanup()

    def create_question(self, *, with_image: bool = False) -> int:
        data: dict[str, object] = {
            "statement": "Quanto é $2 + 2$?",
            "subject": "Matemática",
            "error_reason": "Erro de Cálculo",
            "tags": "aritmética, base",
            "difficulty": "Média",
            "is_objective": "1",
            "correct_option": "D",
            "explanation": "A soma é **4**.",
        }
        if with_image:
            data["question_images"] = (io.BytesIO(PNG_1X1), "questao.png", "image/png")
        response = self.client.post("/review-questions/new", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            return int(conn.execute("SELECT MAX(id) FROM review_questions").fetchone()[0])

    def test_all_secondary_pages_use_base_and_keep_contracts(self) -> None:
        urls = (
            "/database?track=FO&status=unseen&q=Aula",
            "/database?track=UN",
            "/review-questions",
            "/review-questions/new",
            "/review-questions/review",
            "/reprogramming",
        )
        for url in urls:
            response = self.client.get(url)
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn('class="app-header"', page)
            self.assertIn('class="app-skip-link"', page)
            self.assertNotIn("/static/style.css", page)

        database_page = self.client.get("/database?track=FO").get_data(as_text=True)
        for contract in ("data-filter-form", "data-filter-input", "data-filter-target", "data-chip-group", "data-dependent-row", "data-dependent-panel", "data-subject-track", "data-preserve-scroll"):
            self.assertIn(contract, database_page)
        reprogramming_page = self.client.get("/reprogramming").get_data(as_text=True)
        for contract in ("data-reprogramming-app", "data-setting-input", "data-calendar-grid", "data-prev-month", "data-next-month", "data-day-modal", "data-modal-action", "data-run-simulation", "data-apply-simulation", "data-save-settings"):
            self.assertIn(contract, reprogramming_page)

        form_page = self.client.get("/review-questions/new").get_data(as_text=True)
        for contract in ("data-review-question-form", "data-upload-widget", "data-file-input", "data-file-trigger", "data-paste-zone", "data-preview-list", "data-preview-grid", "data-objective-toggle", "data-objective-only"):
            self.assertIn(contract, form_page)

    def test_database_toggle_preserves_query_string(self) -> None:
        return_to = "/database?track=FO&status=unseen&q=Aula&page=1"
        response = self.client.post(
            "/lessons/FO-1/toggle-seen",
            data={"next_date": "2026-07-06", "return_to": return_to},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], return_to)

    def test_question_create_upload_preview_edit_review_and_remove_image(self) -> None:
        question_id = self.create_question(with_image=True)
        for url in (
            f"/review-questions/{question_id}/preview?return_to=/review-questions%3Fq%3D2",
            f"/review-questions/{question_id}/edit?return_to=/review-questions%3Fq%3D2",
            f"/review-questions/review?question_id={question_id}",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn("/static/style.css", response.get_data(as_text=True))

        with connect_db(self.db_path) as conn:
            image_path = conn.execute("SELECT question_image_path FROM review_questions WHERE id=?", (question_id,)).fetchone()[0]
        response = self.client.post(
            f"/review-questions/{question_id}/edit",
            data={
                "statement": "Quanto é 2 + 2?",
                "subject": "Matemática",
                "error_reason": "Erro de Cálculo",
                "tags": "aritmética",
                "difficulty": "Média",
                "is_objective": "1",
                "correct_option": "D",
                "explanation": "Quatro.",
                "remove_question_images": image_path,
            },
        )
        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT question_image_path FROM review_questions WHERE id=?", (question_id,)).fetchone()[0])

        feedback = self.client.post(
            "/review-questions/review",
            data={
                "action": "answer",
                "question_id": str(question_id),
                "forced_question_id": str(question_id),
                "deferred_ids": "",
                "answer_action": "D",
            },
        )
        self.assertEqual(feedback.status_code, 200)
        self.assertIn("Correto.", feedback.get_data(as_text=True))

        listing = self.client.get("/review-questions").get_data(as_text=True)
        self.assertIn("Apagar esta questão?", listing)
        deleted = self.client.post(
            f"/review-questions/{question_id}/delete",
            data={"return_to": "/review-questions?q=2"},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(deleted.headers["Location"], "/review-questions?q=2&deleted=1")

    def test_reprogramming_read_and_temporary_unavailability_apis(self) -> None:
        self.assertEqual(self.client.get("/api/reprogramming/settings").status_code, 200)
        self.assertEqual(self.client.get("/api/reprogramming/unavailability").status_code, 200)
        created = self.client.post("/api/reprogramming/unavailability", json={"date": "2026-07-12"})
        self.assertEqual(created.status_code, 200)
        entry_id = created.get_json()["item"]["id"]
        self.assertEqual(self.client.delete(f"/api/reprogramming/unavailability/{entry_id}").status_code, 200)
        dry_run = self.client.post("/api/reprogramming/dry-run", json={})
        self.assertIn(dry_run.status_code, {200, 400})


if __name__ == "__main__":
    unittest.main()
