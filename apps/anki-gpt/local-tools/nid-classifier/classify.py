#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIORITY = ROOT / "priority_nids.txt"
DESTINATIONS = ROOT / "destinations.txt"
PROMPT = ROOT / "prompt_template.txt"
SEED_CSV = Path.home() / "Desktop" / "anki_nid_classifications.csv"
OUTPUT = Path.home() / "Desktop" / "anki_nid_classifications_v2.csv"
SEARCHES = Path.home() / "Desktop" / "anki_move_searches_v2.txt"
STATE = Path.home() / "Desktop" / "anki_nid_classifier_state_v2.json"
PROFILE = Path.home() / "Library/Application Support/anki-nid-classifier/browser-profile"

NID_RE = re.compile(r"\b\d{13}\b")
CSV_FIELDS = ["batch", "nid", "status", "deck", "reason", "conversation_url"]
QUOTA_RE = [
    re.compile(r"you(?:'ve| have) reached.{0,120}(?:limit|quota)", re.I | re.S),
    re.compile(r"(?:usage|message|rate).{0,40}limit", re.I | re.S),
    re.compile(r"quota.{0,80}(?:reached|exceeded)", re.I | re.S),
    re.compile(r"(?:atingiu|atingido|atingida|excedeu|excedido|excedida).{0,120}(?:limite|cota|quota|teto)", re.I | re.S),
    re.compile(r"(?:limite|cota|quota|teto).{0,120}(?:atingido|atingida|excedido|excedida)", re.I | re.S),
    re.compile(r"try again (?:later|after)", re.I),
    re.compile(r"tente novamente (?:mais tarde|depois)", re.I),
]


def nids_from_text(text: str) -> list[str]:
    out, seen = [], set()
    for nid in NID_RE.findall(text):
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


def read_nids(path: Path) -> list[str]:
    return nids_from_text(path.read_text(encoding="utf-8-sig", errors="replace"))


def read_decks(path: Path) -> list[str]:
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def quota(text: str) -> bool:
    return any(r.search(text or "") for r in QUOTA_RE)


def queue_for(universe: list[str], priority: list[str]) -> list[str]:
    universe_set = set(universe)
    missing = [nid for nid in priority if nid not in universe_set]
    if missing:
        raise SystemExit(f"{len(missing)} NIDs prioritários não estão no universo: {missing[:10]}")
    pset = set(priority)
    return priority + [nid for nid in universe if nid not in pset]


def processed_nids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {
            (r.get("nid") or "").strip()
            for r in csv.DictReader(f)
            if NID_RE.fullmatch((r.get("nid") or "").strip())
        }


def save_state(path: Path, **data) -> None:
    data["updated_at_epoch"] = int(time.time())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_batch(path: Path, rows: list[dict[str, str]]) -> None:
    exists = path.exists() and path.stat().st_size
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


def export_searches(csv_path: Path, output: Path) -> None:
    if not csv_path.exists():
        return
    groups: OrderedDict[str, list[str]] = OrderedDict()
    review: list[str] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            nid = (row.get("nid") or "").strip()
            if (row.get("status") or "").upper() == "CLASSIFIED" and (row.get("deck") or "").strip():
                groups.setdefault(row["deck"].strip(), []).append(nid)
            else:
                review.append(nid)
    lines = []
    for deck, nids in groups.items():
        nids = list(dict.fromkeys(nids))
        lines += [f"## {deck}", f"Quantidade: {len(nids)}",
                  "(" + " OR ".join(f"nid:{n}" for n in nids) + ")", ""]
    lines += ["## NEEDS_REVIEW", ",".join(dict.fromkeys(review)), ""]
    output.write_text("\n".join(lines), encoding="utf-8")


def parse_jsonl(text: str, batch: list[str], decks: list[str]):
    if quota(text):
        return None, "QUOTA"
    cleaned = re.sub(r"```(?:jsonl?|JSONL?)?", "", text).replace("```", "")
    objs = []
    for line in cleaned.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    objs.append(obj)
            except json.JSONDecodeError:
                pass
    if not objs:
        for m in re.finditer(r"\{[^{}\n]*\}", cleaned):
            try:
                objs.append(json.loads(m.group()))
            except json.JSONDecodeError:
                pass

    expected, allowed, by_nid = set(batch), set(decks), {}
    for obj in objs:
        nid = str(obj.get("nid", "")).strip()
        if nid not in expected:
            continue
        if nid in by_nid:
            return None, f"NID duplicado na resposta: {nid}"
        by_nid[nid] = obj

    missing = [nid for nid in batch if nid not in by_nid]
    if missing:
        return None, f"Resposta incompleta: {len(missing)} NIDs ausentes: {missing[:10]}"

    rows = []
    for nid in batch:
        obj = by_nid[nid]
        status = str(obj.get("status", "")).strip().upper()
        deck = str(obj.get("deck", "") or "").strip()
        reason = str(obj.get("reason", "") or "").strip().replace("\n", " ")
        if status not in {"CLASSIFIED", "NEEDS_REVIEW"}:
            return None, f"Status inválido para {nid}: {status}"
        if status == "CLASSIFIED" and deck not in allowed:
            return None, f"Deck inválido para {nid}: {deck}"
        if status == "NEEDS_REVIEW":
            deck = ""
        rows.append({"nid": nid, "status": status, "deck": deck, "reason": reason})
    return rows, None


def browser_executable(explicit: str | None) -> str | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"Browser não encontrado: {p}")
        return str(p)
    for p in [
        Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ]:
        if p.exists():
            return str(p)
    return None


def prompt_box(page, timeout=120):
    selectors = ["#prompt-textarea", 'div[contenteditable="true"][data-virtualkeyboard="true"]',
                 'div[contenteditable="true"]', "textarea"]
    end = time.time() + timeout
    while time.time() < end:
        for selector in selectors:
            loc = page.locator(selector)
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        try:
            if quota(page.locator("body").inner_text(timeout=1500)):
                raise RuntimeError("QUOTA_PAGE")
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("Campo de prompt não apareceu. Confira login e URL do Anki GPT.")


def assistants(page):
    return page.locator('[data-message-author-role="assistant"]')


def wait_response(page, before: int, timeout: int):
    end, last, stable = time.time() + timeout, "", None
    while time.time() < end:
        try:
            alerts = page.locator('[role="alert"]')
            txt = "\n".join(
                alerts.nth(i).inner_text(timeout=1000)
                for i in range(min(alerts.count(), 5))
                if alerts.nth(i).is_visible()
            )
            if quota(txt):
                return "QUOTA", txt
        except Exception:
            pass

        msgs = assistants(page)
        if msgs.count() > before:
            try:
                cur = msgs.last.inner_text(timeout=3000).strip()
            except Exception:
                cur = ""
            if quota(cur):
                return "QUOTA", cur
            if cur and cur == last:
                stable = stable or time.time()
                try:
                    stop = page.locator(
                        'button[data-testid="stop-button"],button[aria-label*="Stop"],button[aria-label*="Parar"]'
                    ).first.is_visible()
                except Exception:
                    stop = False
                if not stop and time.time() - stable >= 4:
                    return "OK", cur
            else:
                last, stable = cur, None

        try:
            body = page.locator("body").inner_text(timeout=1500)
            if quota(body):
                return "QUOTA", body[-4000:]
        except Exception:
            pass
        time.sleep(2)
    return "TIMEOUT", last


def send(page, text: str) -> None:
    box = prompt_box(page)
    box.click()
    try:
        box.fill(text)
    except Exception:
        page.keyboard.insert_text(text)
    page.keyboard.press("Enter")


def render(template: str, batch: list[str], decks: list[str]) -> str:
    return template.format(
        nids_csv=",".join(batch),
        destinations_bullets="\n".join(f"- {d}" for d in decks),
    )


def args():
    p = argparse.ArgumentParser()
    p.add_argument("--gpt-url", default=os.environ.get("ANKI_CLASSIFIER_GPT_URL"))
    p.add_argument("--seed-csv", type=Path, default=SEED_CSV)
    p.add_argument("--all-nids", type=Path)
    p.add_argument("--expected-universe", type=int, default=2457)
    p.add_argument("--priority-nids", type=Path, default=PRIORITY)
    p.add_argument("--destinations", type=Path, default=DESTINATIONS)
    p.add_argument("--prompt-template", type=Path, default=PROMPT)
    p.add_argument("--output", type=Path, default=OUTPUT)
    p.add_argument("--searches-output", type=Path, default=SEARCHES)
    p.add_argument("--state", type=Path, default=STATE)
    p.add_argument("--profile-dir", type=Path, default=PROFILE)
    p.add_argument("--browser-executable", default=os.environ.get("ANKI_CLASSIFIER_BROWSER_EXECUTABLE"))
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--response-timeout", type=int, default=900)
    p.add_argument("--pause-seconds", type=float, default=3)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--validate-config", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--login-only", action="store_true")
    return p.parse_args()


def main() -> int:
    a = args()
    if not 1 <= a.batch_size <= 40:
        raise SystemExit("--batch-size deve ficar entre 1 e 40")

    source = a.all_nids or a.seed_csv
    if not source.exists():
        raise SystemExit(f"Universo não encontrado: {source}")
    universe = read_nids(source)
    priority = read_nids(a.priority_nids)
    decks = read_decks(a.destinations)
    q = queue_for(universe, priority)

    if a.expected_universe and len(universe) != a.expected_universe:
        raise SystemExit(f"Universo={len(universe)}; esperado={a.expected_universe}. Use --expected-universe 0 só se intencional.")

    if a.validate_config:
        print(f"universe_nids: {len(universe)}")
        print(f"priority_nids: {len(priority)}")
        print(f"destinations: {len(decks)}")
        print(f"queue unique: {len(q)}")
        print(f"primeiro NID: {q[0] if q else '-'}")
        print(f"último NID prioritário: {priority[-1] if priority else '-'}")
        print(f"Patologia: {'SIM' if '#UFPR::Biologia::• Patologia' in decks else 'NÃO'}")
        return 0

    if a.reset:
        for p in [a.output, a.searches_output, a.state]:
            p.unlink(missing_ok=True)

    done = processed_nids(a.output)
    remaining = [nid for nid in q if nid not in done]
    print(f"Universo={len(q)} | processados={len(done)} | restantes={len(remaining)} | prioridade={len(priority)}")

    if a.dry_run:
        print(",".join(remaining[:80]))
        return 0
    if not a.gpt_url:
        raise SystemExit("Defina ANKI_CLASSIFIER_GPT_URL ou use --gpt-url.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit("Instale: python3 -m pip install -r requirements.txt") from e

    a.profile_dir.expanduser().mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(a.profile_dir.expanduser()),
            executable_path=browser_executable(a.browser_executable),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(a.gpt_url, wait_until="domcontentloaded", timeout=120_000)

        if a.login_only:
            print("Faça login no browser aberto.")
            input("Depois pressione ENTER aqui para fechar... ")
            return 0

        template = a.prompt_template.read_text(encoding="utf-8")
        batch_no = len(done) // a.batch_size + 1

        while remaining:
            batch = remaining[:a.batch_size]
            print(f"\nBatch {batch_no}: {len(batch)} | {batch[0]} -> {batch[-1]}")
            page.goto(a.gpt_url, wait_until="domcontentloaded", timeout=120_000)

            try:
                prompt_box(page)
            except RuntimeError:
                save_state(a.state, status="PAUSED_QUOTA", batch=batch_no, next_nids=batch,
                           processed=len(done), remaining=len(remaining), page_url=page.url)
                print("Quota detectada antes do envio. PARANDO.")
                return 75

            before = assistants(page).count()
            send(page, render(template, batch, decks))
            result, text = wait_response(page, before, a.response_timeout)
            url = page.url

            if result == "QUOTA" or quota(text):
                save_state(a.state, status="PAUSED_QUOTA", batch=batch_no, next_nids=batch,
                           processed=len(done), remaining=len(remaining), page_url=url,
                           quota_excerpt=text[-2000:])
                print("Quota/teto do GPT detectado. PARANDO imediatamente; batch não gravado.")
                return 75

            if result == "TIMEOUT":
                save_state(a.state, status="STOPPED_TIMEOUT", batch=batch_no, next_nids=batch,
                           processed=len(done), remaining=len(remaining), page_url=url)
                print("Timeout. Batch não gravado.")
                return 2

            parsed, error = parse_jsonl(text, batch, decks)
            if error:
                save_state(a.state, status="STOPPED_INVALID_RESPONSE", batch=batch_no, next_nids=batch,
                           processed=len(done), remaining=len(remaining), page_url=url,
                           error=error, response_excerpt=text[-4000:])
                print(f"Resposta inválida: {error}. Batch não gravado.")
                return 3

            rows = [
                {"batch": str(batch_no), **row, "conversation_url": url}
                for row in parsed
            ]
            append_batch(a.output, rows)
            export_searches(a.output, a.searches_output)
            done.update(batch)
            remaining = [nid for nid in q if nid not in done]
            save_state(a.state, status="RUNNING" if remaining else "COMPLETED",
                       batch=batch_no, processed=len(done), remaining=len(remaining),
                       next_nids=remaining[:a.batch_size], last_conversation_url=url)
            print(f"Batch {batch_no} gravado. Restantes={len(remaining)}")
            batch_no += 1
            if remaining:
                time.sleep(a.pause_seconds)

    print(f"Concluído: {a.output}")
    print(f"Searches: {a.searches_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
