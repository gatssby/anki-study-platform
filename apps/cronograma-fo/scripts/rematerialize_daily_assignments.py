#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.dashboard import build_today_rows, current_local_date


@dataclass(frozen=True)
class RematerializedDate:
    dashboard_date: str
    before: tuple[tuple[str, str], ...]
    after: tuple[tuple[str, str], ...]

    @property
    def changed_count(self) -> int:
        before_map = dict(self.before)
        after_map = dict(self.after)
        keys = set(before_map) | set(after_map)
        return sum(before_map.get(key) != after_map.get(key) for key in keys)


def duplicate_dates(
    conn: sqlite3.Connection,
    *,
    as_of_date: date,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT dashboard_date
        FROM daily_assignments
        WHERE dashboard_date >= ?
          AND assigned_lesson_code IS NOT NULL
          AND assigned_lesson_code <> ''
        GROUP BY dashboard_date, assigned_lesson_code
        HAVING COUNT(*) > 1
        ORDER BY dashboard_date, assigned_lesson_code
        """,
        (as_of_date.isoformat(),),
    ).fetchall()
    return sorted({str(row["dashboard_date"]) for row in rows})


def assignments_for_date(
    conn: sqlite3.Connection,
    dashboard_date: str,
) -> tuple[tuple[str, str], ...]:
    rows = conn.execute(
        """
        SELECT planned_slot_key, assigned_lesson_code
        FROM daily_assignments
        WHERE dashboard_date = ?
        ORDER BY planned_slot_key
        """,
        (dashboard_date,),
    ).fetchall()
    return tuple(
        (str(row["planned_slot_key"]), str(row["assigned_lesson_code"] or ""))
        for row in rows
    )


def snapshot_rows(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(query, parameters).fetchall())


def rematerialize_current_and_future_duplicates(
    conn: sqlite3.Connection,
    *,
    as_of_date: date,
    apply: bool,
) -> list[RematerializedDate]:
    if conn.in_transaction:
        raise RuntimeError("A rematerialização exige uma conexão sem transação pendente.")

    lesson_state_before = snapshot_rows(
        conn,
        "SELECT * FROM lessons ORDER BY slot_key",
    )
    historical_state_before = snapshot_rows(
        conn,
        """
        SELECT dashboard_date, planned_slot_key, assigned_lesson_code, created_at, updated_at
        FROM daily_assignments
        WHERE dashboard_date < ?
        ORDER BY dashboard_date, planned_slot_key
        """,
        (as_of_date.isoformat(),),
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        dates = duplicate_dates(conn, as_of_date=as_of_date)
        results: list[RematerializedDate] = []
        for dashboard_date in dates:
            before = assignments_for_date(conn, dashboard_date)
            build_today_rows(
                conn,
                target_date=dashboard_date,
                as_of_date=as_of_date,
            )
            after = assignments_for_date(conn, dashboard_date)
            results.append(
                RematerializedDate(
                    dashboard_date=dashboard_date,
                    before=before,
                    after=after,
                )
            )

        remaining_duplicates = duplicate_dates(conn, as_of_date=as_of_date)
        if remaining_duplicates:
            raise RuntimeError(
                "Duplicidades atuais/futuras permaneceram após rematerialização: "
                + ", ".join(remaining_duplicates)
            )

        lesson_state_after = snapshot_rows(
            conn,
            "SELECT * FROM lessons ORDER BY slot_key",
        )
        if lesson_state_after != lesson_state_before:
            raise RuntimeError("A rematerialização tentou alterar registros de lessons.")

        historical_state_after = snapshot_rows(
            conn,
            """
            SELECT dashboard_date, planned_slot_key, assigned_lesson_code, created_at, updated_at
            FROM daily_assignments
            WHERE dashboard_date < ?
            ORDER BY dashboard_date, planned_slot_key
            """,
            (as_of_date.isoformat(),),
        )
        if historical_state_after != historical_state_before:
            raise RuntimeError("A rematerialização tentou alterar snapshots históricos.")

        if apply:
            conn.commit()
        else:
            conn.rollback()
        return results
    except Exception:
        conn.rollback()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rematerializa somente daily_assignments duplicados da data atual e futuras."
        ),
    )
    parser.add_argument("--db", required=True, help="Caminho do banco SQLite.")
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        help="Data de corte YYYY-MM-DD; padrão: hoje em America/Sao_Paulo.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Simula e faz rollback.")
    mode.add_argument("--apply", action="store_true", help="Aplica em uma transação única.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db).expanduser().resolve()
    as_of_date = args.as_of_date or current_local_date()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        results = rematerialize_current_and_future_duplicates(
            conn,
            as_of_date=as_of_date,
            apply=bool(args.apply),
        )
    finally:
        conn.close()

    print(f"mode={'apply' if args.apply else 'dry-run'}")
    print(f"as_of_date={as_of_date.isoformat()}")
    print(f"duplicate_dates_processed={len(results)}")
    print(f"assignments_changed={sum(result.changed_count for result in results)}")
    for result in results:
        print(f"dashboard_date={result.dashboard_date}")
        for planned_slot_key, assigned_lesson_code in result.before:
            print(f"before={planned_slot_key}->{assigned_lesson_code}")
        for planned_slot_key, assigned_lesson_code in result.after:
            print(f"after={planned_slot_key}->{assigned_lesson_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
