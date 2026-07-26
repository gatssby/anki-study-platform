from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .paths import APP_DATA_ROOT
from .review_questions import REVIEW_QUESTION_SUBJECTS


DEFAULT_DB_PATH = APP_DATA_ROOT / "cronograma.db"

REVIEW_QUESTION_SUBJECTS_SQL = ", ".join(
    "'{}'".format(subject.replace("'", "''")) for subject in REVIEW_QUESTION_SUBJECTS
)

REQUIRED_TABLE_COLUMNS = {
    "lessons": {
        "slot_key", "lesson_code", "track_code", "lesson_type", "title_raw",
        "portal_title", "relative_path", "external_url", "duration_seconds",
        "subject_name", "subject_prefix", "module_label", "module_number",
        "lesson_number", "week_number", "day_index", "day_name", "slot_index",
        "recommended_date", "is_seen", "seen_at", "is_cut", "cut_reason",
        "cut_source", "source_sheet", "created_at", "updated_at",
    },
    "daily_assignments": {
        "dashboard_date", "planned_slot_key", "assigned_lesson_code",
        "created_at", "updated_at",
    },
    "un_daily_assignments": {
        "dashboard_date", "row_index", "assigned_lesson_code", "created_at",
        "updated_at",
    },
    "exercise_tasks": {
        "id", "source_lesson_code", "scheduled_date", "status", "is_active",
        "manually_moved", "created_at", "updated_at",
    },
    "app_settings": {"setting_key", "setting_value", "updated_at"},
    "schedule_settings": {
        "id", "exam_date", "target_finish_date", "finish_offset_days_before_exam",
        "include_weekends", "include_vacations", "cut_review_free",
        "preserve_english_cut", "auto_adapt_enabled",
        "max_daily_minutes_weekday", "max_daily_minutes_saturday",
        "max_daily_minutes_sunday", "created_at", "updated_at",
    },
    "schedule_unavailability": {
        "id", "start_date", "end_date", "capacity_percent", "reason",
        "created_at", "updated_at",
    },
    "review_questions": {
        "id", "title", "statement", "question_image_path", "question_image_paths",
        "answer_image_path", "subject", "error_reason", "tags", "difficulty",
        "is_objective", "correct_option", "explanation", "is_suspended",
        "next_review_date", "last_reviewed_at", "review_count", "correct_count",
        "wrong_count", "dont_know_count", "created_at", "updated_at",
    },
    "review_question_attempts": {
        "id", "question_id", "reviewed_at", "selected_option", "result",
        "difficulty_after", "next_review_date_after",
    },
}

REQUIRED_INDEXES = {
    "idx_lessons_recommended_date",
    "idx_lessons_track_code",
    "idx_lessons_track",
    "idx_lessons_seen",
    "idx_lessons_cut",
    "idx_daily_assignments_date",
    "idx_un_daily_assignments_date",
    "idx_exercise_tasks_scheduled_date",
    "idx_exercise_tasks_active_status",
    "idx_schedule_unavailability_dates",
    "idx_review_questions_subject",
    "idx_review_questions_pending",
    "idx_review_questions_difficulty",
    "idx_review_question_attempts_question_reviewed",
}

CURRENT_LESSON_TYPE_CONSTRAINT = "lesson_type IN ('lesson', 'list', 'pending', 'review')"
LEGACY_LESSON_TYPE_CONSTRAINT = "lesson_type IN ('lesson', 'pending', 'review')"


@dataclass(frozen=True)
class DatabaseInspection:
    db_path: Path
    exists: bool
    problems: tuple[str, ...]
    actions: tuple[str, ...]

    @property
    def is_runtime_compatible(self) -> bool:
        return self.exists and not self.problems

    @property
    def needs_migration(self) -> bool:
        return bool(self.actions)


class RuntimeDatabaseError(RuntimeError):
    pass


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def inspect_database_connection(
    conn: sqlite3.Connection,
    *,
    db_path: str | Path,
) -> DatabaseInspection:
    path = Path(db_path).resolve()
    problems: list[str] = []
    actions: list[str] = []
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_sql = {str(row["name"]): str(row["sql"] or "") for row in table_rows}
    table_columns: dict[str, set[str]] = {}

    for table_name, required_columns in REQUIRED_TABLE_COLUMNS.items():
        if table_name not in table_sql:
            _append_unique(problems, f"tabela obrigatoria ausente: {table_name}")
            _append_unique(actions, f"criar tabela {table_name}")
            continue
        columns = {
            str(row["name"])
            for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        }
        table_columns[table_name] = columns
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            _append_unique(
                problems,
                f"colunas ausentes em {table_name}: {', '.join(missing_columns)}",
            )
            _append_unique(actions, f"migrar colunas de {table_name}")

    lessons_sql = table_sql.get("lessons", "")
    if lessons_sql and CURRENT_LESSON_TYPE_CONSTRAINT not in lessons_sql:
        _append_unique(problems, "constraint lesson_type de lessons esta desatualizada")
        _append_unique(actions, "reconstruir lessons com a constraint atual")

    review_sql = table_sql.get("review_questions", "")
    expected_subject_tokens = [
        "'{}'".format(subject.replace("'", "''"))
        for subject in REVIEW_QUESTION_SUBJECTS
    ]
    if review_sql and not all(token in review_sql for token in expected_subject_tokens):
        _append_unique(problems, "constraint subject de review_questions esta desatualizada")
        _append_unique(actions, "reconstruir constraint de review_questions")

    existing_indexes = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    missing_indexes = sorted(REQUIRED_INDEXES - existing_indexes)
    if missing_indexes:
        _append_unique(problems, f"indices ausentes: {', '.join(missing_indexes)}")
        _append_unique(actions, "criar indices obrigatorios")

    lesson_columns = table_columns.get("lessons", set())
    if {"track_code", "is_cut", "cut_source"}.issubset(lesson_columns):
        normalization_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM lessons
            WHERE track_code IS NULL OR track_code = ''
               OR is_cut IS NULL
               OR cut_source NOT IN ('manual', 'auto')
            """
        ).fetchone()[0]
        if normalization_count:
            _append_unique(
                problems,
                f"lessons exige normalizacao: {normalization_count} registro(s)",
            )
            _append_unique(actions, "normalizar dados de lessons")

    review_columns = table_columns.get("review_questions", set())
    review_defaults = {
        "is_objective", "is_suspended", "review_count", "correct_count",
        "wrong_count", "dont_know_count", "created_at", "updated_at",
    }
    if review_defaults.issubset(review_columns):
        normalization_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM review_questions
            WHERE is_objective IS NULL OR is_suspended IS NULL
               OR review_count IS NULL OR correct_count IS NULL
               OR wrong_count IS NULL OR dont_know_count IS NULL
               OR created_at IS NULL OR created_at = ''
               OR updated_at IS NULL OR updated_at = ''
            """
        ).fetchone()[0]
        if normalization_count:
            _append_unique(
                problems,
                f"review_questions exige normalizacao: {normalization_count} registro(s)",
            )
            _append_unique(actions, "normalizar dados de review_questions")

    if "schedule_settings" in table_sql and "id" in table_columns.get("schedule_settings", set()):
        settings_exists = conn.execute(
            "SELECT 1 FROM schedule_settings WHERE id = 1"
        ).fetchone()
        if settings_exists is None:
            _append_unique(problems, "registro padrao schedule_settings(id=1) ausente")
            _append_unique(actions, "criar configuracao padrao de agenda")

    try:
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        _append_unique(problems, f"falha ao validar chaves estrangeiras: {exc}")
    else:
        if foreign_key_violations:
            _append_unique(
                problems,
                f"violacoes de chave estrangeira: {len(foreign_key_violations)}",
            )

    return DatabaseInspection(
        db_path=path,
        exists=True,
        problems=tuple(problems),
        actions=tuple(actions),
    )


def inspect_database(db_path: str | Path) -> DatabaseInspection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return DatabaseInspection(
            db_path=path,
            exists=False,
            problems=(f"banco inexistente: {path}",),
            actions=("criar banco e schema atual",),
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        try:
            return inspect_database_connection(conn, db_path=path)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return DatabaseInspection(
            db_path=path,
            exists=True,
            problems=(f"arquivo SQLite invalido ou ilegivel: {exc}",),
            actions=(),
        )


def validate_runtime_database(db_path: str | Path) -> DatabaseInspection:
    inspection = inspect_database(db_path)
    if inspection.is_runtime_compatible:
        return inspection
    details = "; ".join(inspection.problems)
    raise RuntimeDatabaseError(
        f"Banco incompativel para iniciar o app: {details}. "
        "Execute scripts/init_or_migrate_db.py --db CAMINHO --check e, "
        "apos revisar, --apply."
    )


def connect_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _migrate_legacy_lessons_table(conn: sqlite3.Connection) -> bool:
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lessons'"
    ).fetchone()
    table_sql = table_sql_row["sql"] if table_sql_row else None
    if not table_sql or LEGACY_LESSON_TYPE_CONSTRAINT not in table_sql:
        return False
    if conn.in_transaction:
        raise RuntimeError(
            "A migracao legada de lessons exige conexao sem transacao pendente."
        )

    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS lessons_new")
        conn.execute(
            """
            CREATE TABLE lessons_new (
                slot_key TEXT PRIMARY KEY,
                lesson_code TEXT NOT NULL UNIQUE,
                track_code TEXT NOT NULL DEFAULT 'FO',
                lesson_type TEXT NOT NULL CHECK (lesson_type IN ('lesson', 'list', 'pending', 'review')),
                title_raw TEXT NOT NULL,
                portal_title TEXT,
                relative_path TEXT,
                external_url TEXT,
                duration_seconds INTEGER,
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
                is_seen INTEGER NOT NULL DEFAULT 0 CHECK (is_seen IN (0, 1)),
                seen_at TEXT,
                is_cut INTEGER NOT NULL DEFAULT 0 CHECK (is_cut IN (0, 1)),
                cut_reason TEXT,
                cut_source TEXT CHECK (cut_source IN ('manual', 'auto') OR cut_source IS NULL),
                source_sheet TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO lessons_new (
                slot_key,
                lesson_code,
                track_code,
                lesson_type,
                title_raw,
                portal_title,
                relative_path,
                external_url,
                duration_seconds,
                subject_name,
                subject_prefix,
                module_label,
                module_number,
                lesson_number,
                week_number,
                day_index,
                day_name,
                slot_index,
                recommended_date,
                is_seen,
                seen_at,
                is_cut,
                cut_reason,
                cut_source,
                source_sheet,
                created_at,
                updated_at
            )
            SELECT
                slot_key,
                lesson_code,
                track_code,
                lesson_type,
                title_raw,
                NULL AS portal_title,
                relative_path,
                NULL AS external_url,
                NULL AS duration_seconds,
                subject_name,
                subject_prefix,
                module_label,
                module_number,
                lesson_number,
                week_number,
                day_index,
                day_name,
                slot_index,
                recommended_date,
                is_seen,
                seen_at,
                0 AS is_cut,
                NULL AS cut_reason,
                NULL AS cut_source,
                source_sheet,
                created_at,
                updated_at
            FROM lessons
            """
        )
        conn.execute("DROP TABLE lessons")
        conn.execute("ALTER TABLE lessons_new RENAME TO lessons")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {1 if foreign_keys_enabled else 0}")
    return True


def init_db(
    conn: sqlite3.Connection,
    *,
    allow_migrations: bool = False,
) -> None:
    existing_app_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if str(row[0]) in REQUIRED_TABLE_COLUMNS
    }
    if existing_app_tables and not allow_migrations:
        database_row = conn.execute("PRAGMA database_list").fetchone()
        database_path = str(database_row[2] or "<conexao SQLite>")
        original_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            inspection = inspect_database_connection(conn, db_path=database_path)
        finally:
            conn.row_factory = original_row_factory
        if not inspection.is_runtime_compatible:
            details = "; ".join(inspection.problems)
            raise RuntimeDatabaseError(
                f"Banco existente exige migracao explicita: {details}. "
                "Use scripts/init_or_migrate_db.py --db CAMINHO --check/--apply."
            )

    if allow_migrations:
        _migrate_legacy_lessons_table(conn)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            slot_key TEXT PRIMARY KEY,
            lesson_code TEXT NOT NULL UNIQUE,
            track_code TEXT NOT NULL DEFAULT 'FO',
            lesson_type TEXT NOT NULL CHECK (lesson_type IN ('lesson', 'list', 'pending', 'review')),
            title_raw TEXT NOT NULL,
            portal_title TEXT,
            relative_path TEXT,
            external_url TEXT,
            duration_seconds INTEGER,
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
            is_seen INTEGER NOT NULL DEFAULT 0 CHECK (is_seen IN (0, 1)),
            seen_at TEXT,
            is_cut INTEGER NOT NULL DEFAULT 0 CHECK (is_cut IN (0, 1)),
            cut_reason TEXT,
            cut_source TEXT CHECK (cut_source IN ('manual', 'auto') OR cut_source IS NULL),
            source_sheet TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_assignments (
            dashboard_date TEXT NOT NULL,
            planned_slot_key TEXT NOT NULL,
            assigned_lesson_code TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (dashboard_date, planned_slot_key)
        );

        CREATE TABLE IF NOT EXISTS un_daily_assignments (
            dashboard_date TEXT NOT NULL,
            row_index INTEGER NOT NULL,
            assigned_lesson_code TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (dashboard_date, row_index)
        );

        CREATE TABLE IF NOT EXISTS exercise_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_lesson_code TEXT NOT NULL UNIQUE,
            scheduled_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'skipped')),
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
            manually_moved INTEGER NOT NULL DEFAULT 0 CHECK (manually_moved IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_lesson_code) REFERENCES lessons(lesson_code) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS schedule_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            exam_date TEXT,
            target_finish_date TEXT,
            finish_offset_days_before_exam INTEGER,
            include_weekends INTEGER NOT NULL DEFAULT 0 CHECK (include_weekends IN (0, 1)),
            include_vacations INTEGER NOT NULL DEFAULT 0 CHECK (include_vacations IN (0, 1)),
            cut_review_free INTEGER NOT NULL DEFAULT 1 CHECK (cut_review_free IN (0, 1)),
            preserve_english_cut INTEGER NOT NULL DEFAULT 1 CHECK (preserve_english_cut IN (0, 1)),
            auto_adapt_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_adapt_enabled IN (0, 1)),
            max_daily_minutes_weekday INTEGER NOT NULL DEFAULT 240,
            max_daily_minutes_saturday INTEGER NOT NULL DEFAULT 180,
            max_daily_minutes_sunday INTEGER NOT NULL DEFAULT 180,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS schedule_unavailability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            capacity_percent INTEGER NOT NULL CHECK (capacity_percent >= 0 AND capacity_percent <= 100),
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            statement TEXT,
            question_image_path TEXT,
            question_image_paths TEXT,
            answer_image_path TEXT,
            subject TEXT NOT NULL CHECK (subject IN ({REVIEW_QUESTION_SUBJECTS_SQL})),
            error_reason TEXT CHECK (
                error_reason IN ('Desatenção', 'Lacuna Teórica', 'Erro de Cálculo', 'Interpretação', 'Outro')
                OR error_reason IS NULL
            ),
            tags TEXT,
            difficulty TEXT NOT NULL CHECK (
                difficulty IN ('Muito difícil', 'Difícil', 'Média', 'Fácil', 'Muito fácil')
            ),
            is_objective INTEGER NOT NULL DEFAULT 1 CHECK (is_objective IN (0, 1)),
            correct_option TEXT CHECK (correct_option IN ('A', 'B', 'C', 'D', 'E') OR correct_option IS NULL),
            explanation TEXT,
            is_suspended INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
            next_review_date TEXT,
            last_reviewed_at TEXT,
            review_count INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            wrong_count INTEGER NOT NULL DEFAULT 0,
            dont_know_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_question_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            reviewed_at TEXT NOT NULL,
            selected_option TEXT CHECK (selected_option IN ('A', 'B', 'C', 'D', 'E') OR selected_option IS NULL),
            result TEXT NOT NULL CHECK (
                result IN (
                    'correct',
                    'wrong',
                    'dont_know',
                    'skipped_session',
                    'postponed_tomorrow',
                    'self_correct',
                    'self_wrong'
                )
            ),
            difficulty_after TEXT CHECK (
                difficulty_after IN ('Muito difícil', 'Difícil', 'Média', 'Fácil', 'Muito fácil')
                OR difficulty_after IS NULL
            ),
            next_review_date_after TEXT,
            FOREIGN KEY (question_id) REFERENCES review_questions(id) ON DELETE CASCADE
        );

        """.format(REVIEW_QUESTION_SUBJECTS_SQL=REVIEW_QUESTION_SUBJECTS_SQL)
    )

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(lessons)").fetchall()
    }
    if "track_code" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN track_code TEXT NOT NULL DEFAULT 'FO'")
    if "relative_path" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN relative_path TEXT")
    if "external_url" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN external_url TEXT")
    if "duration_seconds" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN duration_seconds INTEGER")
    if "portal_title" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN portal_title TEXT")
    if "is_cut" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN is_cut INTEGER NOT NULL DEFAULT 0 CHECK (is_cut IN (0, 1))")
    if "cut_reason" not in columns:
        conn.execute("ALTER TABLE lessons ADD COLUMN cut_reason TEXT")
    if "cut_source" not in columns:
        conn.execute(
            "ALTER TABLE lessons ADD COLUMN cut_source TEXT CHECK (cut_source IN ('manual', 'auto') OR cut_source IS NULL)"
        )
    ensure_review_question_columns(conn)
    ensure_review_question_subject_constraint(conn)
    conn.execute("UPDATE lessons SET track_code = 'FO' WHERE track_code IS NULL OR track_code = ''")
    conn.execute("UPDATE lessons SET is_cut = 0 WHERE is_cut IS NULL")
    conn.execute("UPDATE lessons SET cut_source = NULL WHERE cut_source NOT IN ('manual', 'auto')")
    conn.execute("UPDATE review_questions SET is_objective = 1 WHERE is_objective IS NULL")
    conn.execute("UPDATE review_questions SET is_suspended = 0 WHERE is_suspended IS NULL")
    conn.execute("UPDATE review_questions SET review_count = 0 WHERE review_count IS NULL")
    conn.execute("UPDATE review_questions SET correct_count = 0 WHERE correct_count IS NULL")
    conn.execute("UPDATE review_questions SET wrong_count = 0 WHERE wrong_count IS NULL")
    conn.execute("UPDATE review_questions SET dont_know_count = 0 WHERE dont_know_count IS NULL")
    conn.execute("UPDATE review_questions SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL OR created_at = ''")
    conn.execute("UPDATE review_questions SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL OR updated_at = ''")
    conn.execute(
        """
        INSERT INTO schedule_settings (
            id,
            exam_date,
            target_finish_date,
            finish_offset_days_before_exam,
            include_weekends,
            include_vacations,
            cut_review_free,
            preserve_english_cut,
            auto_adapt_enabled,
            max_daily_minutes_weekday,
            max_daily_minutes_saturday,
            max_daily_minutes_sunday,
            updated_at
        )
        VALUES (1, NULL, NULL, NULL, 0, 0, 1, 1, 1, 240, 180, 180, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO NOTHING
        """
    )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_lessons_recommended_date
            ON lessons(recommended_date);

        CREATE INDEX IF NOT EXISTS idx_lessons_track_code
            ON lessons(track_code);

        CREATE INDEX IF NOT EXISTS idx_lessons_track
            ON lessons(track_code, subject_prefix, module_number, recommended_date, day_index, slot_index);

        CREATE INDEX IF NOT EXISTS idx_lessons_seen
            ON lessons(is_seen, recommended_date);

        CREATE INDEX IF NOT EXISTS idx_lessons_cut
            ON lessons(is_cut, recommended_date);

        CREATE INDEX IF NOT EXISTS idx_daily_assignments_date
            ON daily_assignments(dashboard_date);

        CREATE INDEX IF NOT EXISTS idx_un_daily_assignments_date
            ON un_daily_assignments(dashboard_date);

        CREATE INDEX IF NOT EXISTS idx_exercise_tasks_scheduled_date
            ON exercise_tasks(scheduled_date);

        CREATE INDEX IF NOT EXISTS idx_exercise_tasks_active_status
            ON exercise_tasks(is_active, status, scheduled_date);

        CREATE INDEX IF NOT EXISTS idx_schedule_unavailability_dates
            ON schedule_unavailability(start_date, end_date);

        CREATE INDEX IF NOT EXISTS idx_review_questions_subject
            ON review_questions(subject);

        CREATE INDEX IF NOT EXISTS idx_review_questions_pending
            ON review_questions(is_suspended, next_review_date);

        CREATE INDEX IF NOT EXISTS idx_review_questions_difficulty
            ON review_questions(difficulty);

        CREATE INDEX IF NOT EXISTS idx_review_question_attempts_question_reviewed
            ON review_question_attempts(question_id, reviewed_at);
        """
    )

    conn.commit()


def initialize_or_migrate_database(
    db_path: str | Path,
) -> tuple[DatabaseInspection, DatabaseInspection]:
    path = Path(db_path).expanduser().resolve()
    before = inspect_database(path)
    if before.needs_migration:
        conn = connect_db(path)
        try:
            init_db(conn, allow_migrations=True)
        finally:
            conn.close()
    after = inspect_database(path)
    return before, after


def ensure_review_question_columns(conn: sqlite3.Connection) -> None:
    review_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(review_questions)").fetchall()
    }
    if "title" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN title TEXT")
    if "statement" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN statement TEXT")
    if "question_image_path" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN question_image_path TEXT")
    if "question_image_paths" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN question_image_paths TEXT")
    if "answer_image_path" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN answer_image_path TEXT")
    if "subject" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN subject TEXT")
    if "error_reason" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN error_reason TEXT")
    if "tags" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN tags TEXT")
    if "difficulty" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN difficulty TEXT")
    if "is_objective" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN is_objective INTEGER NOT NULL DEFAULT 1")
    if "correct_option" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN correct_option TEXT")
    if "explanation" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN explanation TEXT")
    if "is_suspended" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0")
    if "next_review_date" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN next_review_date TEXT")
    if "last_reviewed_at" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN last_reviewed_at TEXT")
    if "review_count" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0")
    if "correct_count" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN correct_count INTEGER NOT NULL DEFAULT 0")
    if "wrong_count" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN wrong_count INTEGER NOT NULL DEFAULT 0")
    if "dont_know_count" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN dont_know_count INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN created_at TEXT")
    if "updated_at" not in review_columns:
        conn.execute("ALTER TABLE review_questions ADD COLUMN updated_at TEXT")

    attempt_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(review_question_attempts)").fetchall()
    }
    if "question_id" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN question_id INTEGER")
    if "reviewed_at" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN reviewed_at TEXT")
    if "selected_option" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN selected_option TEXT")
    if "result" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN result TEXT")
    if "difficulty_after" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN difficulty_after TEXT")
    if "next_review_date_after" not in attempt_columns:
        conn.execute("ALTER TABLE review_question_attempts ADD COLUMN next_review_date_after TEXT")


def ensure_review_question_subject_constraint(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_questions'"
    ).fetchone()
    table_sql = str(row["sql"] or "") if row else ""
    expected_tokens = ["'{}'".format(subject.replace("'", "''")) for subject in REVIEW_QUESTION_SUBJECTS]
    if table_sql and all(token in table_sql for token in expected_tokens):
        return

    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        conn.execute("SAVEPOINT review_questions_subject_migration")
        conn.execute("DROP TABLE IF EXISTS review_questions_new")
        conn.execute(
            """
            CREATE TABLE review_questions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                statement TEXT,
                question_image_path TEXT,
                question_image_paths TEXT,
                answer_image_path TEXT,
                subject TEXT NOT NULL CHECK (subject IN ({subjects_sql})),
                error_reason TEXT CHECK (
                    error_reason IN ('Desatenção', 'Lacuna Teórica', 'Erro de Cálculo', 'Interpretação', 'Outro')
                    OR error_reason IS NULL
                ),
                tags TEXT,
                difficulty TEXT NOT NULL CHECK (
                    difficulty IN ('Muito difícil', 'Difícil', 'Média', 'Fácil', 'Muito fácil')
                ),
                is_objective INTEGER NOT NULL DEFAULT 1 CHECK (is_objective IN (0, 1)),
                correct_option TEXT CHECK (correct_option IN ('A', 'B', 'C', 'D', 'E') OR correct_option IS NULL),
                explanation TEXT,
                is_suspended INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
                next_review_date TEXT,
                last_reviewed_at TEXT,
                review_count INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count INTEGER NOT NULL DEFAULT 0,
                dont_know_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """.format(subjects_sql=REVIEW_QUESTION_SUBJECTS_SQL)
        )
        conn.execute(
            """
            INSERT INTO review_questions_new (
                id,
                title,
                statement,
                question_image_path,
                question_image_paths,
                answer_image_path,
                subject,
                error_reason,
                tags,
                difficulty,
                is_objective,
                correct_option,
                explanation,
                is_suspended,
                next_review_date,
                last_reviewed_at,
                review_count,
                correct_count,
                wrong_count,
                dont_know_count,
                created_at,
                updated_at
            )
            SELECT
                id,
                title,
                statement,
                question_image_path,
                question_image_paths,
                answer_image_path,
                subject,
                error_reason,
                tags,
                difficulty,
                is_objective,
                correct_option,
                explanation,
                is_suspended,
                next_review_date,
                last_reviewed_at,
                review_count,
                correct_count,
                wrong_count,
                dont_know_count,
                created_at,
                updated_at
            FROM review_questions
            """
        )
        conn.execute("DROP TABLE review_questions")
        conn.execute("ALTER TABLE review_questions_new RENAME TO review_questions")
        conn.execute("RELEASE SAVEPOINT review_questions_subject_migration")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT review_questions_subject_migration")
        conn.execute("RELEASE SAVEPOINT review_questions_subject_migration")
        conn.execute("DROP TABLE IF EXISTS review_questions_new")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")
