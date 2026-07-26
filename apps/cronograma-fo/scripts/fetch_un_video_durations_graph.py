from __future__ import annotations

import csv
import json
import ssl
import time
import certifi
import configparser
import urllib.request
import urllib.parse
import urllib.error
import subprocess
from pathlib import Path
from socket import timeout as SocketTimeout


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_final_lesson_list.csv"
OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "output" / "un_video_durations.csv"

RCLONE_CONF = Path("/Users/gatsby/.config/rclone/rclone.conf")
RCLONE_REMOTE = "onedrive:"


def load_access_token() -> str:
    cp = configparser.ConfigParser()
    cp.read(RCLONE_CONF, encoding="utf-8")
    token_blob = json.loads(cp["onedrive"]["token"])
    return token_blob["access_token"]


def force_rclone_token_refresh() -> str:
    subprocess.run(
        ["rclone", "lsf", RCLONE_REMOTE, "--max-depth", "1"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return load_access_token()


def graph_get_video_metadata(access_token: str, video_relative_path: str, ctx: ssl.SSLContext) -> dict:
    graph_path = f"/Universo Narrado/{video_relative_path}"
    encoded_path = urllib.parse.quote(graph_path, safe="/:")
    select = "id,name,size,video,file,webUrl"
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{encoded_path}?$select={urllib.parse.quote(select, safe=',')}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")

    with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_existing_results(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    return {
        row["video_relative_path"]: row
        for row in rows
        if row.get("video_relative_path")
    }


def save_results(path: Path, rows_by_path: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = [rows_by_path[k] for k in sorted(rows_by_path.keys())]

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["video_relative_path", "duration_ms", "duration_seconds", "status", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    access_token = load_access_token()
    ctx = ssl.create_default_context(cafile=certifi.where())

    with INPUT_CSV.open("r", encoding="utf-8", newline="") as f:
        source_rows = list(csv.DictReader(f))

    seen = set()
    video_paths = []
    for row in source_rows:
        path = row["video_relative_path"].strip()
        if not path.lower().endswith(".mp4"):
            continue
        if path in seen:
            continue
        seen.add(path)
        video_paths.append(path)

    results = load_existing_results(OUTPUT_CSV)

    total = len(video_paths)
    already_done = sum(
        1 for p in video_paths
        if p in results and results[p].get("duration_seconds")
    )

    print(f"Total de vídeos: {total}")
    print(f"Já com duração salva: {already_done}")

    transient_http_codes = {429, 500, 502, 503, 504}

    for i, path in enumerate(video_paths, start=1):
        if path in results and results[path].get("duration_seconds"):
            if i % 50 == 0 or i == total:
                print(f"[{i}/{total}] SKIP")
            continue

        max_attempts = 5
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                data = graph_get_video_metadata(access_token, path, ctx)
                video_facet = data.get("video") or {}
                duration_ms = video_facet.get("duration")
                duration_seconds = round(duration_ms / 1000, 3) if duration_ms is not None else ""

                results[path] = {
                    "video_relative_path": path,
                    "duration_ms": duration_ms if duration_ms is not None else "",
                    "duration_seconds": duration_seconds,
                    "status": "ok" if duration_seconds != "" else "no_video_duration",
                    "error": "",
                }

                if i % 25 == 0 or i == total:
                    print(f"[{i}/{total}] OK")

                save_results(OUTPUT_CSV, results)
                time.sleep(0.08)
                break

            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print(f"[{i}/{total}] Token expirou, renovando via rclone...")
                    access_token = force_rclone_token_refresh()
                    time.sleep(1)
                    continue

                if e.code in transient_http_codes and attempt < max_attempts:
                    wait = 2 ** attempt
                    print(f"[{i}/{total}] HTTP {e.code}, retry em {wait}s...")
                    time.sleep(wait)
                    continue

                results[path] = {
                    "video_relative_path": path,
                    "duration_ms": "",
                    "duration_seconds": "",
                    "status": "error",
                    "error": f"HTTP {e.code}",
                }
                print(f"[{i}/{total}] ERRO HTTP {e.code}: {path}")
                save_results(OUTPUT_CSV, results)
                break

            except (TimeoutError, SocketTimeout) as e:
                if attempt < max_attempts:
                    wait = 2 ** attempt
                    print(f"[{i}/{total}] TIMEOUT, retry em {wait}s...")
                    time.sleep(wait)
                    continue

                results[path] = {
                    "video_relative_path": path,
                    "duration_ms": "",
                    "duration_seconds": "",
                    "status": "error",
                    "error": "timeout",
                }
                print(f"[{i}/{total}] ERRO TIMEOUT: {path}")
                save_results(OUTPUT_CSV, results)
                break

            except Exception as e:
                if attempt < max_attempts:
                    wait = 2 ** attempt
                    print(f"[{i}/{total}] ERRO transitório, retry em {wait}s... -> {e}")
                    time.sleep(wait)
                    continue

                results[path] = {
                    "video_relative_path": path,
                    "duration_ms": "",
                    "duration_seconds": "",
                    "status": "error",
                    "error": str(e),
                }
                print(f"[{i}/{total}] ERRO FINAL: {path} -> {e}")
                save_results(OUTPUT_CSV, results)
                break

    save_results(OUTPUT_CSV, results)

    filled = sum(1 for p in video_paths if results.get(p, {}).get("duration_seconds"))
    errors = sum(1 for p in video_paths if results.get(p, {}).get("status") == "error")

    print("\nConcluído.")
    print(f"Total: {total}")
    print(f"Com duração: {filled}")
    print(f"Com erro: {errors}")
    print(f"Arquivo: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
