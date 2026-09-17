#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIORITY = ROOT / "priority_nids.txt"
DEST = ROOT / "destinations.txt"
NID_RE = re.compile(r"\b\d{13}\b")


def extract(text: str) -> list[str]:
    out = []
    seen = set()
    for nid in NID_RE.findall(text):
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


def write_nids(path: Path, nids: list[str]) -> None:
    path.write_text("\n".join(nids) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--priority-from-file", type=Path)
    p.add_argument("--priority-from-stdin", action="store_true")
    p.add_argument("--add-destination")
    p.add_argument("--remove-destination")
    p.add_argument("--validate", action="store_true")
    p.add_argument(
        "--seed-csv",
        type=Path,
        default=Path.home() / "Desktop" / "anki_nid_classifications.csv",
        help="CSV antigo usado como universo dos mesmos cards",
    )
    args = p.parse_args()

    if args.priority_from_file:
        nids = extract(args.priority_from_file.read_text(encoding="utf-8", errors="replace"))
        write_nids(PRIORITY, nids)
        print(f"priority_nids.txt atualizado: {len(nids)} NIDs")

    if args.priority_from_stdin:
        nids = extract(sys.stdin.read())
        write_nids(PRIORITY, nids)
        print(f"priority_nids.txt atualizado: {len(nids)} NIDs")

    decks = [
        x.strip()
        for x in DEST.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]

    if args.add_destination:
        if args.add_destination not in decks:
            decks.append(args.add_destination)
            DEST.write_text("\n".join(decks) + "\n", encoding="utf-8")
            print(f"Destino adicionado: {args.add_destination}")
        else:
            print("Destino já existe.")

    if args.remove_destination:
        if args.remove_destination in decks:
            decks = [d for d in decks if d != args.remove_destination]
            DEST.write_text("\n".join(decks) + "\n", encoding="utf-8")
            print(f"Destino removido: {args.remove_destination}")
        else:
            print("Destino não encontrado.")

    if args.validate:
        priority = extract(PRIORITY.read_text(encoding="utf-8"))
        decks = [
            x.strip()
            for x in DEST.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        universe = (
            extract(args.seed_csv.read_text(encoding="utf-8-sig", errors="replace"))
            if args.seed_csv.exists()
            else []
        )
        missing = [n for n in priority if n not in set(universe)] if universe else []
        print(f"seed_csv_exists={'SIM' if args.seed_csv.exists() else 'NÃO'}")
        print(f"universe_from_seed_csv={len(universe)}")
        print(f"priority={len(priority)} unique={len(set(priority))}")
        print(f"destinations={len(decks)} unique={len(set(decks))}")
        print(f"priority_missing_from_universe={len(missing)}")
        print(f"pathology={'SIM' if '#UFPR::Biologia::• Patologia' in decks else 'NÃO'}")
        if args.seed_csv.exists() and missing:
            print("Faltantes:", ",".join(missing[:20]))
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
