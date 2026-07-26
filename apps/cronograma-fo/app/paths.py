from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_app_data_root() -> Path:
    configured_db_path = os.environ.get("CRONOGRAMA_DB_PATH")
    if configured_db_path:
        return Path(configured_db_path).expanduser().resolve().parent
    return (PROJECT_ROOT / "data").resolve()


APP_DATA_ROOT = resolve_app_data_root()
LEGACY_APP_STATE_ROOT = (PROJECT_ROOT / "state" / "federal_online").resolve()
LEGACY_REVIEW_QUESTION_UPLOADS_DIR = LEGACY_APP_STATE_ROOT / "review_questions" / "uploads"
