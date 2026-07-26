#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fo_session_security import protect_storage_state, save_storage_state
from fo_sync_materials import (
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_ENV_FILE,
    DEFAULT_NAV_TIMEOUT_MS,
    Logger,
    RuntimeConfig,
    build_runtime_config,
    login_if_needed,
    neutralize_intro_overlay,
    now_utc_iso,
    page_requires_login,
    summarize_exception,
)

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - handled at runtime.
    PlaywrightTimeoutError = RuntimeError  # type: ignore[assignment]
    sync_playwright = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CRONOGRAMA_FO_STATE_ROOT = PROJECT_ROOT / "state" / "federal_online"
LEGACY_SESSION_FILE = PROJECT_ROOT / "work" / "fo_bridge" / "session" / "fo_storage_state.json"
DEFAULT_COURSE_URL = (
    "https://www.federalonline.com.br/portal/curso-aula/"
    "produto-pacote/57b0cfa5139851ce785e14e9bd584626/extensivo-ufpr-2026"
)

OUTPUT_SUBDIR = "cronograma_fo"
BUILD_PREFIX = ".build_aulas_index_"
CHECKPOINT_FILENAME = "checkpoint.json"
SCHEMA_VERSION = 1
MAX_SESSION_REFRESHES = 5
VIDEO_ITEM_SELECTOR = ".body-list li, li[onclick*='openMedia4'], [onclick*='openMedia4']"

TSV_COLUMNS = [
    "area",
    "portal_root",
    "portal_subject",
    "disciplina",
    "ordem",
    "nome_real_aula",
    "titulo_original",
    "duracao_segundos",
    "duracao_texto",
    "media_id",
    "item_id",
    "media_type",
    "course_url",
    "source_ref",
    "captured_at",
    "status",
    "error",
    "video_stream_url",
]

IGNORE_KEYWORDS = (
    "lista de exercicios",
    "resumo",
    "slides",
    "gabarito",
)

ACADEMIC_ROOT_AREAS = (
    "Aulas Gerais",
    "Específicas",
    "Disciplinas Opcionais",
    "Conteúdos Extras",
)

GENERIC_SUBJECT_LABELS = (
    "aulas",
    "lista de aulas",
    "videos",
    "videoaulas",
)

OPERATIONAL_ITEM_KEYWORDS = (
    "bem-vindo ao federal online",
    "comece por aqui",
    "como enviar redacoes",
    "como digitalizar redacoes",
)

TOLERATED_TIMEOUT_ROOTS = (
    "Disciplinas Opcionais",
    "Conteúdos Extras",
)

TOLERATED_TIMEOUT_LABELS = (
    "Literatura Geral",
    "Sociologia Geral",
    "Filosofia Geral",
    "Inglês",
    "Espanhol",
)

MAIN_PLACEHOLDER_ROOTS = (
    "Aulas Gerais",
    "Específicas",
)

NOT_LAUNCHED_MARKERS = (
    "em atualizacao",
    "em breve",
    "aguardando",
    "nao lancada",
    "nao lancado",
)

M3U8_URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+?\.m3u8(?:\?[^\s\"'<>\\)]*)?", re.IGNORECASE)
VIDEO_URL_VALUE_RE = re.compile(
    r"\bvideo_url\b\s*[:=]\s*([\"'])(?P<url>.*?\.m3u8(?:\?.*?)?)\1",
    re.IGNORECASE | re.DOTALL,
)


class SessionExpiredDuringCollection(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta e normaliza o indice semanal de videoaulas do Federal Online."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Arquivo env com credenciais FO. Padrao: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_CRONOGRAMA_FO_STATE_ROOT,
        help=f"Diretorio de estado do Cronograma FO. Padrao: {DEFAULT_CRONOGRAMA_FO_STATE_ROOT}",
    )
    parser.add_argument(
        "--course-url",
        default="",
        help="URL do curso. Se omitida, usa o DEFAULT_COURSE_URL deste script.",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=None,
        help="Arquivo storage_state Playwright alternativo.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Executa o navegador com interface grafica.",
    )
    parser.add_argument(
        "--nav-timeout-ms",
        type=int,
        default=DEFAULT_NAV_TIMEOUT_MS,
        help=f"Timeout de navegacao em ms. Padrao: {DEFAULT_NAV_TIMEOUT_MS}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita a quantidade de aulas processadas. Uso recomendado apenas para teste.",
    )
    parser.add_argument(
        "--path-filter",
        action="append",
        default=[],
        help=(
            "Coleta somente ramos cujo caminho/source_ref contenha este texto. "
            "Pode ser repetido. Para melhor poda, use o caminho a partir da raiz."
        ),
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Permite promover indice vazio. Por padrao, indice vazio falha.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignora checkpoint existente e inicia uma coleta nova.",
    )
    parser.add_argument(
        "--cleanup-age-hours",
        type=float,
        default=24.0,
        help="Idade minima, em horas, para remover builds temporarios antigos.",
    )
    return parser.parse_args()


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_key(value: str | None) -> str:
    import unicodedata

    text = normalize_space(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def normalize_label(value: str | None) -> str:
    text = normalize_space(value)
    return re.sub(r"\s+\d+%$", "", text).strip()


def clean_path_filters(values: list[str] | None) -> list[str]:
    return [value for value in (normalize_space(item) for item in values or []) if value]


def path_filter_slug(path_filters: list[str]) -> str:
    if not path_filters:
        return ""
    slug_parts = [
        re.sub(r"[^a-z0-9]+", "_", normalize_key(path_filter)).strip("_")
        for path_filter in path_filters
    ]
    slug = "_".join(part for part in slug_parts if part)
    return slug[:120] or "filtered"


def path_filter_allows_visit(path_label: str, path_filters: list[str]) -> bool:
    if not path_filters:
        return True
    path_key = normalize_key(path_label)
    for path_filter in path_filters:
        filter_key = normalize_key(path_filter)
        if not filter_key:
            continue
        if ">" not in path_filter:
            return True
        if path_key in filter_key or filter_key in path_key:
            return True
    return False


def path_filter_allows_collect(path_label: str, path_filters: list[str]) -> bool:
    if not path_filters:
        return True
    path_key = normalize_key(path_label)
    return any(normalize_key(path_filter) in path_key for path_filter in path_filters)


def strip_lesson_prefix(value: str | None) -> str:
    text = normalize_space(value)
    return (
        re.sub(r"^\s*Aula\s*\d+\s*[-–—:]\s*", "", text, flags=re.IGNORECASE).strip()
        or text
    )


def normalize_sidebar_title(value: str | None) -> str:
    text = normalize_space(value)
    if not text:
        return ""
    text = re.sub(r"\s+Video\s*\..*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Vídeo\s*\..*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Material.*$", "", text, flags=re.IGNORECASE)
    return normalize_space(text)


def parse_duration_seconds(raw_value: str | None) -> int | None:
    value = normalize_space(raw_value)
    if not value:
        return None
    if value.isdigit():
        return int(value)

    clock_match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)", value)
    if clock_match:
        hours = int(clock_match.group(1) or 0)
        minutes = int(clock_match.group(2) or 0)
        seconds = int(clock_match.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds

    duration_match = re.fullmatch(
        r"(?:(\d+)\s*h(?:oras?)?\s*)?"
        r"(?:(\d+)\s*min(?:utos?)?\s*)?"
        r"(?:(\d+)\s*s(?:eg(?:undos?)?)?\s*)?",
        value,
        flags=re.IGNORECASE,
    )
    if duration_match and any(duration_match.groups()):
        hours = int(duration_match.group(1) or 0)
        minutes = int(duration_match.group(2) or 0)
        seconds = int(duration_match.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds

    return None


def extract_duration_text(raw_value: str | None) -> str:
    text = normalize_space(raw_value)
    if not text:
        return ""

    clock_match = re.search(r"\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b", text)
    if clock_match:
        return clock_match.group(0)

    duration_match = re.search(
        r"(?:(?:\d+)\s*h(?:oras?)?\s*)?"
        r"(?:(?:\d+)\s*min(?:utos?)?\s*)"
        r"(?:(?:\d+)\s*s(?:eg(?:undos?)?)?\s*)?",
        text,
        flags=re.IGNORECASE,
    )
    if duration_match:
        return normalize_space(duration_match.group(0))

    seconds_match = re.search(r"\b\d+\s*s(?:eg(?:undos?)?)?\b", text, flags=re.IGNORECASE)
    if seconds_match:
        return normalize_space(seconds_match.group(0))

    return ""


def normalize_hls_candidate(value: str | None) -> str:
    text = normalize_space(value)
    if not text:
        return ""
    text = html.unescape(text)
    text = (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\x2F", "/")
        .replace("\\x2f", "/")
    )
    return text.rstrip(".,;")


def extract_hls_urls_from_text(value: str | None) -> list[str]:
    text = normalize_hls_candidate(value)
    if not text:
        return []

    candidates: list[str] = []
    for match in VIDEO_URL_VALUE_RE.finditer(text):
        candidates.append(match.group("url"))
    candidates.extend(match.group(0) for match in M3U8_URL_RE.finditer(text))

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = normalize_hls_candidate(candidate)
        if not url or ".m3u8" not in url.casefold() or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def choose_video_stream_url(urls: list[str]) -> str | None:
    unique_urls: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = normalize_hls_candidate(raw_url)
        if not url or ".m3u8" not in url.casefold() or url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    if not unique_urls:
        return None

    def score(url: str) -> tuple[int, int]:
        url_key = url.casefold()
        score_value = 0
        if "/master.m3u8" in url_key:
            score_value += 400
        elif url_key.split("?", 1)[0].endswith("master.m3u8"):
            score_value += 300
        if "/dlx/" in url_key:
            score_value += 100
        if any(marker in url_key for marker in ("/segment", "/chunk", "/frag")):
            score_value -= 50
        return score_value, -len(url)

    return max(unique_urls, key=score)


def collect_m3u8_urls_from_network(page: Any) -> tuple[list[str], list[tuple[str, Any]]]:
    urls: list[str] = []
    seen: set[str] = set()

    def remember(raw_url: str | None) -> None:
        for url in extract_hls_urls_from_text(raw_url):
            if url not in seen:
                seen.add(url)
                urls.append(url)

    def on_request(request: Any) -> None:
        try:
            remember(request.url)
        except Exception:
            pass

    def on_response(response: Any) -> None:
        try:
            remember(response.url)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    return urls, [("request", on_request), ("response", on_response)]


def detach_network_listeners(page: Any, listeners: list[tuple[str, Any]]) -> None:
    for event_name, handler in listeners:
        try:
            page.remove_listener(event_name, handler)
        except Exception:
            pass


def extract_video_stream_url(
    page: Any,
    logger: Logger,
    *,
    network_urls: list[str] | None = None,
    source_ref: str = "",
    title: str = "",
) -> str | None:
    html_urls: list[str] = []
    try:
        frames = list(page.frames)
        for frame in frames:
            try:
                html_urls.extend(extract_hls_urls_from_text(frame.url))
                html_urls.extend(extract_hls_urls_from_text(frame.content()))
            except Exception:
                continue
    except Exception as exc:
        logger.log(
            "[video_stream] falha ao inspecionar frames "
            f"source_ref={source_ref or '-'} titulo={title or '-'} detalhe={summarize_exception(exc)}"
        )

    selected = choose_video_stream_url(html_urls)
    if selected:
        logger.log(f"[video_stream] encontrado via frame source_ref={source_ref} titulo={title or '-'}")
        return selected

    selected = choose_video_stream_url(network_urls or [])
    if selected:
        logger.log(f"[video_stream] encontrado via network source_ref={source_ref} titulo={title or '-'}")
        return selected

    logger.log(f"[video_stream] nao encontrado source_ref={source_ref} titulo={title or '-'}")
    return None


def format_duration_text(seconds: int | None) -> str:
    if seconds is None:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}min")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def split_source_ref(subject_group: str) -> tuple[str, str]:
    hierarchy = parse_portal_hierarchy(subject_group)
    return hierarchy["portal_root"], hierarchy["disciplina"]


def parse_portal_hierarchy(subject_group: str) -> dict[str, str]:
    parts = [part.strip() for part in subject_group.split(">") if part.strip()]
    if not parts:
        return {"portal_root": "", "portal_subject": "", "disciplina": ""}

    portal_root = parts[0]
    portal_subject = portal_root
    generic_labels = {normalize_key(label) for label in GENERIC_SUBJECT_LABELS}
    if (
        len(parts) >= 3
        and normalize_key(parts[0]) == normalize_key("Específicas")
        and normalize_key(parts[1]) == normalize_key("Redação")
        and normalize_key(parts[2]) == normalize_key("UFPR")
    ):
        portal_subject = f"{parts[1]} {parts[2]}"
    else:
        for candidate in reversed(parts[1:]):
            if normalize_key(candidate) not in generic_labels:
                portal_subject = candidate
                break

    if normalize_key(portal_root) == normalize_key("Conteúdos Extras"):
        disciplina = f"{portal_subject} - Extras"
    else:
        disciplina = portal_subject

    return {
        "portal_root": portal_root,
        "portal_subject": portal_subject,
        "disciplina": disciplina,
    }


def is_academic_source_ref(subject_group: str) -> bool:
    area = parse_portal_hierarchy(subject_group)["portal_root"]
    allowed_roots = {normalize_key(area_name) for area_name in ACADEMIC_ROOT_AREAS}
    return normalize_key(area) in allowed_roots


def item_text_key(item_info: dict[str, str | None]) -> str:
    return " ".join(
        normalize_key(value)
        for value in (
            item_info.get("title"),
            item_info.get("clickedText"),
            item_info.get("rawText"),
        )
        if value
    )


def is_main_placeholder_source_ref(subject_group: str) -> bool:
    area = parse_portal_hierarchy(subject_group)["portal_root"]
    allowed_roots = {normalize_key(root) for root in MAIN_PLACEHOLDER_ROOTS}
    return normalize_key(area) in allowed_roots


def is_not_launched_placeholder(subject_group: str, item_info: dict[str, str | None]) -> bool:
    if not is_main_placeholder_source_ref(subject_group):
        return False

    text = item_text_key(item_info)
    if not re.search(r"\baula\s*\d+\b", text):
        return False

    return any(marker in text for marker in NOT_LAUNCHED_MARKERS)


def is_timeout_exception(exc: Exception) -> bool:
    detail = summarize_exception(exc)
    return isinstance(exc, PlaywrightTimeoutError) or "timeout" in detail.casefold()


def is_tolerated_timeout_branch(subject_group: str) -> bool:
    hierarchy = parse_portal_hierarchy(subject_group)
    root_key = normalize_key(hierarchy["portal_root"])
    tolerated_roots = {normalize_key(root) for root in TOLERATED_TIMEOUT_ROOTS}
    if root_key in tolerated_roots:
        return True

    subject_key = normalize_key(subject_group)
    return any(normalize_key(label) in subject_key for label in TOLERATED_TIMEOUT_LABELS)


def should_tolerate_branch_exception(subject_group: str, exc: Exception) -> bool:
    return is_tolerated_timeout_branch(subject_group) and is_timeout_exception(exc)


def log_tolerated_branch_timeout(logger: Logger, subject_group: str, step: str, exc: Exception) -> None:
    logger.log(
        "[skip] timeout tolerado em ramo nao principal "
        f"step={step} path={subject_group} detalhe={summarize_exception(exc)}"
    )


def is_ignored_item(item_info: dict[str, str | None]) -> bool:
    haystack = " ".join(
        normalize_key(value)
        for value in (
            item_info.get("title"),
            item_info.get("clickedText"),
            item_info.get("rawText"),
        )
        if value
    )
    return any(keyword in haystack for keyword in IGNORE_KEYWORDS)


def is_operational_item(item_info: dict[str, str | None]) -> bool:
    haystack = " ".join(
        normalize_key(value)
        for value in (
            item_info.get("title"),
            item_info.get("clickedText"),
            item_info.get("rawText"),
        )
        if value
    )
    return any(keyword in haystack for keyword in OPERATIONAL_ITEM_KEYWORDS)


def classify_unavailable(item_info: dict[str, str | None], exc: Exception) -> tuple[str, str]:
    text = item_text_key(item_info)
    detail = summarize_exception(exc)
    if any(marker in text for marker in NOT_LAUNCHED_MARKERS):
        return "not_launched", detail
    return "error", detail


def build_config(args: argparse.Namespace) -> RuntimeConfig:
    course_url_effective = args.course_url or DEFAULT_COURSE_URL
    runtime_args = SimpleNamespace(
        env_file=args.env_file,
        state_root=args.state_root,
        headed=args.headed,
        nav_timeout_ms=args.nav_timeout_ms,
        download_timeout_ms=DEFAULT_DOWNLOAD_TIMEOUT_MS,
    )

    if args.env_file.exists():
        return replace(build_runtime_config(runtime_args), course_url=course_url_effective)

    return RuntimeConfig(
        env_file=args.env_file,
        state_root=args.state_root,
        storage_state_file=args.state_root / "storage_state.json",
        raw_dir=args.state_root / "raw",
        files_dir=args.state_root / "raw" / "files",
        logs_dir=args.state_root / "raw" / "logs",
        log_file=args.state_root / "raw" / "logs" / "fo_sync_aulas.log",
        persistent_logs_dir=args.state_root / "logs",
        persistent_log_file=args.state_root / "logs" / "fo_sync_aulas.latest.log",
        course_url=course_url_effective,
        login_url=os.environ.get("FEDERAL_ONLINE_LOGIN_URL"),
        email=os.environ.get("FEDERAL_ONLINE_EMAIL", ""),
        password=os.environ.get("FEDERAL_ONLINE_PASSWORD", ""),
        headless=not args.headed,
        nav_timeout_ms=args.nav_timeout_ms,
        download_timeout_ms=DEFAULT_DOWNLOAD_TIMEOUT_MS,
    )


def choose_storage_state(args: argparse.Namespace, config: RuntimeConfig, logger: Logger) -> Path | None:
    candidates = [
        args.storage_state,
        config.storage_state_file,
        LEGACY_SESSION_FILE,
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            protect_storage_state(candidate)
            logger.log(f"Usando storage_state: {candidate}")
            return candidate
    logger.log("Nenhum storage_state existente encontrado; a execucao dependera de login automatico.")
    return None


def read_current_video(page: Any) -> dict[str, str | None]:
    return page.evaluate(
        """
        () => ({
            nomeAula: document.querySelector('#nomeAula')?.value || null,
            idVideo: document.querySelector('#idVideo')?.value || null,
            controleDuracao: document.querySelector('#controleDuracao')?.value || null,
            embedSrc: document.querySelector('#embed')?.src || null,
            tipoVideo: document.querySelector('#tipoVideo')?.value || null
        })
        """
    )


def wait_until(predicate: Any, timeout_seconds: float, error_message: str) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError(error_message)


def body_is_loaded(body: Any) -> bool:
    return bool(
        body.evaluate(
            """
            (el) => {
                const text = (el.innerText || '').trim();
                if (text.includes('Aguarde carregando')) {
                    return false;
                }
                return el.children.length > 0 || text.length > 0 || el.classList.contains('show');
            }
            """
        )
    )


def resolve_descendant_path(container: Any, path: list[int]) -> Any:
    node = container
    for index in path:
        node = node.locator(":scope > *").nth(index)
    return node


def list_logical_descendant_paths(
    container: Any,
    target_selector: str,
    blocked_ancestor_selector: str | None = None,
) -> list[list[int]]:
    return container.evaluate(
        """
        (el, args) => {
            const { targetSelector, blockedAncestorSelector } = args;

            const buildPath = (node) => {
                const path = [];
                let current = node;

                while (current && current !== el) {
                    const parent = current.parentElement;
                    if (!parent) {
                        return null;
                    }

                    path.unshift(Array.prototype.indexOf.call(parent.children, current));
                    current = parent;
                }

                return current === el ? path : null;
            };

            const hasBlockedAncestor = (node) => {
                if (!blockedAncestorSelector) {
                    return false;
                }

                let current = node.parentElement;
                while (current && current !== el) {
                    if (current.matches(blockedAncestorSelector)) {
                        return true;
                    }
                    current = current.parentElement;
                }

                return false;
            };

            return [...el.querySelectorAll(targetSelector)]
                .filter((node) => !hasBlockedAncestor(node))
                .map((node) => buildPath(node))
                .filter(Boolean);
        }
        """,
        {
            "targetSelector": target_selector,
            "blockedAncestorSelector": blocked_ancestor_selector,
        },
    )


def list_video_item_paths(body: Any) -> list[list[int]]:
    return body.evaluate(
        """
        (el, args) => {
            const { targetSelector } = args;

            const buildPath = (node) => {
                const path = [];
                let current = node;

                while (current && current !== el) {
                    const parent = current.parentElement;
                    if (!parent) {
                        return null;
                    }

                    path.unshift(Array.prototype.indexOf.call(parent.children, current));
                    current = parent;
                }

                return current === el ? path : null;
            };

            const hasNestedCardAncestor = (node) => {
                let current = node.parentElement;
                while (current && current !== el) {
                    if (current.matches('.card')) {
                        return true;
                    }
                    current = current.parentElement;
                }
                return false;
            };

            const unique = [];
            const seen = new Set();

            for (const rawNode of el.querySelectorAll(targetSelector)) {
                const closestListItem = rawNode.closest('li');
                const node = closestListItem && el.contains(closestListItem)
                    ? closestListItem
                    : rawNode;

                if (hasNestedCardAncestor(node)) {
                    continue;
                }

                const path = buildPath(node);
                if (!path) {
                    continue;
                }

                const key = path.join('/');
                if (seen.has(key)) {
                    continue;
                }
                seen.add(key);
                unique.push(path);
            }

            return unique;
        }
        """,
        {"targetSelector": VIDEO_ITEM_SELECTOR},
    )


def scroll_container_to_load_more(page: Any, container: Any) -> None:
    container.evaluate(
        """
        (el) => {
            const scrollNodes = [
                document.scrollingElement || document.documentElement,
                el,
                ...el.querySelectorAll('.card-body, .body-list, [style*="overflow"]')
            ];

            for (const node of scrollNodes) {
                try {
                    node.scrollTop = node.scrollHeight;
                } catch (_) {}
            }

            const candidates = el.querySelectorAll('.body-list li, li[onclick*="openMedia4"], [onclick*="openMedia4"]');
            const last = candidates[candidates.length - 1];
            if (last) {
                last.scrollIntoView({ block: 'end', inline: 'nearest' });
            }
        }
        """
    )
    page.wait_for_timeout(300)


def wait_for_video_items_stable(page: Any, body: Any, logger: Logger, subject_group: str) -> None:
    previous_count = -1
    stable_hits = 0

    for _ in range(8):
        item_paths = list_video_item_paths(body)
        current_count = len(item_paths)
        if current_count == previous_count:
            stable_hits += 1
        else:
            stable_hits = 0
        if stable_hits >= 2:
            return

        previous_count = current_count
        scroll_container_to_load_more(page, body)

    logger.log(
        f"[tree] lista de aulas estabilizou por limite de tentativas source_ref={subject_group} "
        f"itens={previous_count}"
    )


def wait_for_child_cards_stable(page: Any, container: Any, logger: Logger, path_label: str) -> None:
    previous_count = -1
    stable_hits = 0

    for _ in range(6):
        card_paths = list_logical_descendant_paths(
            container=container,
            target_selector=".card",
            blocked_ancestor_selector=".card",
        )
        current_count = len(card_paths)
        if current_count == previous_count:
            stable_hits += 1
        else:
            stable_hits = 0
        if stable_hits >= 2:
            return

        previous_count = current_count
        scroll_container_to_load_more(page, container)

    logger.log(
        f"[tree] lista de blocos estabilizou por limite de tentativas path={path_label} "
        f"blocos={previous_count}"
    )


def get_card_title(card: Any) -> str:
    title = normalize_label(card.locator(":scope > .card-header").inner_text())
    if title:
        return title

    return normalize_label(
        card.evaluate(
            """
            (el) => {
                const header = el.querySelector(':scope > .card-header .title');
                return header?.innerText || '';
            }
            """
        )
    )


def expand_card(page: Any, logger: Logger, card: Any, path_label: str) -> Any:
    header = card.locator(":scope > .card-header")
    body = card.locator(":scope > .card-body")
    body_class = body.get_attribute("class") or ""

    if "show" not in body_class:
        logger.log(f"[tree] abrindo bloco={path_label}")
        header.scroll_into_view_if_needed()
        header.click()
        neutralize_intro_overlay(page, logger)

    wait_until(
        lambda: "show" in (body.get_attribute("class") or ""),
        timeout_seconds=15,
        error_message=f"Timeout ao expandir o bloco '{path_label}'.",
    )
    wait_until(
        lambda: body_is_loaded(body),
        timeout_seconds=20,
        error_message=f"Timeout aguardando conteudo do bloco '{path_label}'.",
    )

    return body


def extract_item_info(item: Any) -> dict[str, str | None]:
    return item.evaluate(
        """
        (el) => {
            const cleanTitle = (text) => (text || '')
                .replace(/\\s+/g, ' ')
                .replace(/\\s+Vídeo\\s*\\..*$/i, '')
                .replace(/\\s+Video\\s*\\..*$/i, '')
                .replace(/\\s+Material.*$/i, '')
                .trim();

            const parseOpenMedia4 = (onclick) => {
                const match = (onclick || '').match(/openMedia4\\s*\\(\\s*event\\s*,\\s*(['"])(.*?)\\1\\s*,\\s*(['"])(.*?)\\3\\s*,\\s*(['"])(.*?)\\5\\s*\\)/i);
                if (!match) return {};
                return {
                    itemId: match[2] || null,
                    onclickTitle: match[4] || null,
                    mediaType: match[6] || null,
                };
            };

            const rawText = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            const clickedText = rawText.replace(/^Marcar como visto\\s*/i, '').trim();
            const clickable = el.matches('[onclick]') ? el : el.querySelector('[onclick]');
            const onclick = clickable?.getAttribute('onclick') || el.getAttribute('onclick') || '';
            const parsed = parseOpenMedia4(onclick);
            const videoNode = el.querySelector('[data-video]') || clickable?.querySelector('[data-video]');
            const dataVideo = videoNode?.getAttribute('data-video') || null;

            const spanTexts = [...el.querySelectorAll('span')]
                .map((node) => cleanTitle(node.innerText || ''))
                .filter(Boolean);

            const explicitTitle =
                parsed.onclickTitle ||
                spanTexts.find((text) => /^Aula\\s+/i.test(text)) ||
                null;
            const fallbackTitle = cleanTitle(clickedText);

            let mediaType = parsed.mediaType || null;
            if (!mediaType && dataVideo) {
                mediaType = 'V';
            }

            return {
                rawText,
                clickedText,
                title: explicitTitle || fallbackTitle || null,
                dataVideo,
                itemId: parsed.itemId || null,
                mediaType,
                onclick,
            };
        }
        """
    )


def wait_for_selected_video(
    page: Any,
    previous_state: dict[str, str | None],
    expected_id: str | None,
    expected_title: str | None,
) -> dict[str, str | None]:
    previous_id = normalize_space(previous_state.get("idVideo"))
    previous_title = normalize_space(previous_state.get("nomeAula"))
    expected_title_key = normalize_key(expected_title)
    stable_match_hits = 0

    deadline = time.time() + 20
    while time.time() < deadline:
        if page_requires_login(page):
            raise SessionExpiredDuringCollection("Redirecionado para login enquanto aguardava aula carregar.")
        current = read_current_video(page)
        current_id = normalize_space(current.get("idVideo"))
        current_title = normalize_space(current.get("nomeAula"))
        current_title_key = normalize_key(current_title)

        if current_id and previous_id and current_id != previous_id:
            return current
        if current_id and not previous_id:
            return current

        title_matches = bool(expected_title_key and current_title_key == expected_title_key)
        id_matches = bool(expected_id and current_id == expected_id)

        if title_matches:
            stable_match_hits += 1
            if current_id and (current_id != previous_id or id_matches or stable_match_hits >= 4):
                return current
        else:
            stable_match_hits = 0

        time.sleep(0.25)

    if page_requires_login(page):
        raise SessionExpiredDuringCollection("Redirecionado para login enquanto aguardava aula carregar.")
    current = read_current_video(page)
    current_id = normalize_space(current.get("idVideo"))
    current_title = normalize_space(current.get("nomeAula"))
    if expected_title_key and normalize_key(current_title) == expected_title_key:
        return current
    if expected_id and current_id == expected_id:
        return current

    raise RuntimeError(
        "Timeout aguardando a aula carregar apos o clique. "
        f"Anterior: id={previous_id or '-'} titulo={previous_title or '-'} | "
        f"Esperado: id_hint={expected_id or '-'} titulo={expected_title or '-'}."
    )


def build_entry(
    *,
    subject_group: str,
    order: int,
    item_info: dict[str, str | None],
    current: dict[str, str | None] | None,
    course_url: str,
    captured_at: str,
    status: str,
    error: str,
    video_stream_url: str | None = None,
) -> dict[str, Any]:
    hierarchy = parse_portal_hierarchy(subject_group)
    area = hierarchy["portal_root"]
    portal_subject = hierarchy["portal_subject"]
    disciplina = hierarchy["disciplina"]
    title_hint = normalize_sidebar_title(item_info.get("title")) or normalize_sidebar_title(
        item_info.get("clickedText")
    )
    titulo_original = normalize_space(current.get("nomeAula") if current else None) or title_hint
    raw_duration_text = extract_duration_text(item_info.get("clickedText")) or extract_duration_text(
        item_info.get("rawText")
    )
    duration_seconds = parse_duration_seconds(current.get("controleDuracao") if current else None)
    if duration_seconds is None:
        duration_seconds = parse_duration_seconds(raw_duration_text)
    duration_text = raw_duration_text or format_duration_text(duration_seconds)

    if status == "ok" and duration_seconds is None:
        status = "duration_missing"
        error = error or "Duracao ausente em #controleDuracao e no texto do item."

    return {
        "area": area,
        "portal_root": area,
        "portal_subject": portal_subject,
        "disciplina": disciplina,
        "ordem": order,
        "nome_real_aula": strip_lesson_prefix(titulo_original),
        "titulo_original": titulo_original,
        "duracao_segundos": duration_seconds,
        "duracao_texto": duration_text,
        "media_id": normalize_space(current.get("idVideo") if current else None)
        or normalize_space(item_info.get("dataVideo")),
        "item_id": normalize_space(item_info.get("itemId")),
        "media_type": normalize_space(current.get("tipoVideo") if current else None)
        or normalize_space(item_info.get("mediaType")),
        "course_url": course_url,
        "source_ref": subject_group,
        "captured_at": captured_at,
        "status": status,
        "error": error,
        "video_stream_url": video_stream_url,
    }


def collect_direct_videos(
    *,
    page: Any,
    logger: Logger,
    body: Any,
    subject_group: str,
    course_url: str,
    captured_at: str,
    entries: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    checkpoint_writer: Any,
    limit: int | None,
) -> bool:
    wait_for_video_items_stable(page, body, logger, subject_group)
    item_paths = list_video_item_paths(body)

    if not item_paths:
        return False

    if not is_academic_source_ref(subject_group):
        logger.log(f"[skip] bloco fora do indice academico source_ref={subject_group}")
        return False

    video_order = 0
    for item_path in item_paths:
        if page_requires_login(page):
            raise SessionExpiredDuringCollection(
                f"Redirecionado para login antes de processar item em {subject_group}."
            )
        try:
            item = resolve_descendant_path(body, item_path)
            item_info = extract_item_info(item)
        except Exception as exc:
            if page_requires_login(page):
                raise SessionExpiredDuringCollection(
                    f"Redirecionado para login ao ler item em {subject_group}."
                ) from exc
            raise

        if is_ignored_item(item_info):
            continue
        if is_operational_item(item_info):
            skipped_title = normalize_sidebar_title(
                item_info.get("title") or item_info.get("clickedText")
            )
            logger.log(f"[skip] item operacional source_ref={subject_group} item={skipped_title}")
            continue
        is_video = item_info.get("mediaType") == "V"
        is_placeholder = is_not_launched_placeholder(subject_group, item_info)
        if not is_video and not is_placeholder:
            continue

        video_order += 1
        title_hint = normalize_sidebar_title(item_info.get("title")) or normalize_sidebar_title(
            item_info.get("clickedText")
        )
        dedupe_value = (
            normalize_space(item_info.get("itemId"))
            or normalize_space(item_info.get("dataVideo"))
            or normalize_key(title_hint)
            or str(video_order)
        )
        dedupe_key = (subject_group, dedupe_value)
        if dedupe_key in seen_keys:
            logger.log(f"[skip] duplicado source_ref={subject_group} item={title_hint or dedupe_value}")
            continue

        if is_placeholder and not is_video:
            entry = build_entry(
                subject_group=subject_group,
                order=video_order,
                item_info=item_info,
                current=None,
                course_url=course_url,
                captured_at=captured_at,
                status="not_launched",
                error="Aula existe no portal, mas ainda nao possui video disponivel.",
                video_stream_url=None,
            )
            entries.append(entry)
            seen_keys.add(dedupe_key)
            checkpoint_writer()
            logger.log(
                "[write] "
                f"status=not_launched source_ref={subject_group} ordem={video_order} "
                f"titulo={entry['titulo_original'] or title_hint or '-'}"
            )
            if limit is not None and len(entries) >= limit:
                return True
            continue

        previous_state = read_current_video(page)
        item.scroll_into_view_if_needed()
        network_urls, network_listeners = collect_m3u8_urls_from_network(page)
        try:
            item.click()
            current = wait_for_selected_video(
                page=page,
                previous_state=previous_state,
                expected_id=normalize_space(item_info.get("dataVideo")) or None,
                expected_title=title_hint or None,
            )
            page.wait_for_timeout(500)
            video_stream_url = extract_video_stream_url(
                page,
                logger,
                network_urls=network_urls,
                source_ref=subject_group,
                title=title_hint,
            )
            entry = build_entry(
                subject_group=subject_group,
                order=video_order,
                item_info=item_info,
                current=current,
                course_url=course_url,
                captured_at=captured_at,
                status="ok",
                error="",
                video_stream_url=video_stream_url,
            )
            entries.append(entry)
            seen_keys.add(dedupe_key)
            checkpoint_writer()
            logger.log(
                "[write] "
                f"status={entry['status']} source_ref={subject_group} ordem={video_order} "
                f"media_id={entry['media_id'] or '-'} titulo={entry['titulo_original']}"
            )
        except Exception as exc:
            if page_requires_login(page):
                raise SessionExpiredDuringCollection(
                    f"Redirecionado para login ao processar {subject_group} :: {title_hint or dedupe_value}."
                ) from exc
            status, error = classify_unavailable(item_info, exc)
            entry = build_entry(
                subject_group=subject_group,
                order=video_order,
                item_info=item_info,
                current=None,
                course_url=course_url,
                captured_at=captured_at,
                status=status,
                error=error,
                video_stream_url=None,
            )
            entries.append(entry)
            seen_keys.add(dedupe_key)
            checkpoint_writer()
            logger.log(
                "[warn] "
                f"status={status} source_ref={subject_group} ordem={video_order} "
                f"titulo={entry['titulo_original'] or title_hint or '-'} detalhe={error}"
            )
        finally:
            detach_network_listeners(page, network_listeners)

        if limit is not None and len(entries) >= limit:
            return True

    return False


def walk_cards(
    *,
    page: Any,
    logger: Logger,
    container: Any,
    breadcrumbs: list[str],
    path_filters: list[str],
    course_url: str,
    captured_at: str,
    entries: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    checkpoint_writer: Any,
    limit: int | None,
) -> bool:
    path_label = " > ".join(breadcrumbs) if breadcrumbs else "(raiz)"
    try:
        wait_for_child_cards_stable(page, container, logger, path_label)
    except Exception as exc:
        if page_requires_login(page):
            raise SessionExpiredDuringCollection(
                f"Redirecionado para login ao listar blocos em {path_label}."
            ) from exc
        if should_tolerate_branch_exception(path_label, exc):
            log_tolerated_branch_timeout(logger, path_label, "listar_blocos", exc)
            return False
        raise

    card_paths = list_logical_descendant_paths(
        container=container,
        target_selector=".card",
        blocked_ancestor_selector=".card",
    )

    for card_path in card_paths:
        card = resolve_descendant_path(container, card_path)
        title = get_card_title(card)
        current_path = breadcrumbs + ([title] if title else [])
        path_label = " > ".join(current_path) if current_path else "(sem titulo)"
        if not path_filter_allows_visit(path_label, path_filters):
            continue

        try:
            body = expand_card(page, logger, card, path_label)
        except Exception as exc:
            if page_requires_login(page):
                raise SessionExpiredDuringCollection(
                    f"Redirecionado para login ao abrir bloco {path_label}."
                ) from exc
            logger.log(f"[warn] bloco ignorado path={path_label} detalhe={summarize_exception(exc)}")
            continue

        if path_filter_allows_collect(path_label, path_filters):
            try:
                should_stop = collect_direct_videos(
                    page=page,
                    logger=logger,
                    body=body,
                    subject_group=path_label,
                    course_url=course_url,
                    captured_at=captured_at,
                    entries=entries,
                    seen_keys=seen_keys,
                    checkpoint_writer=checkpoint_writer,
                    limit=limit,
                )
            except Exception as exc:
                if page_requires_login(page):
                    raise SessionExpiredDuringCollection(
                        f"Redirecionado para login ao coletar videos em {path_label}."
                    ) from exc
                if should_tolerate_branch_exception(path_label, exc):
                    log_tolerated_branch_timeout(logger, path_label, "coletar_videos", exc)
                    continue
                raise
            if should_stop:
                return True

        try:
            should_stop = walk_cards(
                page=page,
                logger=logger,
                container=body,
                breadcrumbs=current_path,
                path_filters=path_filters,
                course_url=course_url,
                captured_at=captured_at,
                entries=entries,
                seen_keys=seen_keys,
                checkpoint_writer=checkpoint_writer,
                limit=limit,
            )
        except Exception as exc:
            if page_requires_login(page):
                raise SessionExpiredDuringCollection(
                    f"Redirecionado para login ao percorrer subblocos em {path_label}."
                ) from exc
            if should_tolerate_branch_exception(path_label, exc):
                log_tolerated_branch_timeout(logger, path_label, "percorrer_subblocos", exc)
                continue
            raise
        if should_stop:
            return True

    return False


def resolve_collection_root(page: Any, config: RuntimeConfig, logger: Logger) -> Any:
    try:
        page.wait_for_function(
            """
            () => {
              return (
                document.querySelector("#topicsAccordion") ||
                document.querySelector(".card") ||
                document.querySelector(".header-wrapper") ||
                document.querySelector(".body-list li")
              );
            }
            """,
            timeout=config.nav_timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        if page_requires_login(page):
            raise SessionExpiredDuringCollection("Redirecionado para login antes de localizar a arvore.")
        raise RuntimeError(
            "A pagina do curso abriu, mas nenhum seletor da arvore de aulas foi encontrado "
            "(#topicsAccordion, .card, .header-wrapper ou .body-list li). "
            "Verifique se a sessao esta autenticada e se a URL abriu o curso correto."
        ) from exc

    if page.locator("#topicsAccordion").count() > 0:
        logger.log("[tree] raiz de coleta=#topicsAccordion")
        return page.locator("#topicsAccordion")

    logger.log("[tree] #topicsAccordion ausente; usando body como raiz da travessia legada.")
    return page.locator("body")


def cleanup_old_builds(output_dir: Path, keep_dir: Path | None, age_hours: float, logger: Logger) -> None:
    if not output_dir.exists():
        return
    threshold = time.time() - (age_hours * 3600)
    for candidate in output_dir.glob(f"{BUILD_PREFIX}*"):
        if keep_dir and candidate == keep_dir:
            continue
        try:
            if candidate.stat().st_mtime > threshold:
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            logger.log(f"Temporario antigo removido: {candidate}")
        except OSError as exc:
            logger.log(f"[warn] falha ao remover temporario={candidate} detalhe={summarize_exception(exc)}")


def checkpoint_seen_key_from_entry(entry: dict[str, Any]) -> tuple[str, str] | None:
    source_ref = normalize_space(str(entry.get("source_ref") or ""))
    if not source_ref:
        return None
    dedupe_value = (
        normalize_space(str(entry.get("item_id") or ""))
        or normalize_space(str(entry.get("media_id") or ""))
        or normalize_key(str(entry.get("titulo_original") or ""))
        or normalize_space(str(entry.get("ordem") or ""))
    )
    if not dedupe_value:
        return None
    return source_ref, dedupe_value


def build_status_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(entry.get("status") or "") for entry in entries))


def write_checkpoint(
    checkpoint_path: Path,
    *,
    course_url: str,
    path_filters: list[str],
    entries: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    logger: Logger,
) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_at": now_utc_iso(),
        "course_url": course_url,
        "path_filters": path_filters,
        "total": len(entries),
        "status_counts": build_status_counts(entries),
        "seen_keys": [list(key) for key in sorted(seen_keys)],
        "entries": entries,
    }
    tmp_path = checkpoint_path.with_name(f".{checkpoint_path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, checkpoint_path)
    logger.log(f"[checkpoint] progresso gravado total={len(entries)} path={checkpoint_path}")


def load_checkpoint(
    checkpoint_path: Path,
    *,
    course_url: str,
    path_filters: list[str],
    logger: Logger,
    resume_enabled: bool,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    if not resume_enabled:
        logger.log("[checkpoint] retomada desativada por --no-resume; iniciando do zero.")
        return [], set()
    if not checkpoint_path.exists():
        logger.log("[checkpoint] nenhum checkpoint encontrado; iniciando do zero.")
        return [], set()

    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.log(f"[checkpoint] checkpoint invalido; iniciando do zero. detalhe={summarize_exception(exc)}")
        return [], set()

    if payload.get("schema_version") != SCHEMA_VERSION:
        logger.log("[checkpoint] schema_version incompativel; iniciando do zero.")
        return [], set()
    if payload.get("course_url") != course_url:
        logger.log(
            "[checkpoint] course_url diferente do curso efetivo; iniciando do zero. "
            f"checkpoint={payload.get('course_url')} effective={course_url}"
        )
        return [], set()
    checkpoint_path_filters = payload.get("path_filters") or []
    if not isinstance(checkpoint_path_filters, list):
        checkpoint_path_filters = []
    if clean_path_filters(checkpoint_path_filters) != path_filters:
        logger.log(
            "[checkpoint] path_filters diferente do filtro efetivo; iniciando do zero. "
            f"checkpoint={payload.get('path_filters') or []} effective={path_filters}"
        )
        return [], set()

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        logger.log("[checkpoint] entries ausente/invalido; iniciando do zero.")
        return [], set()
    entries = [entry for entry in raw_entries if isinstance(entry, dict)]
    for entry in entries:
        entry.setdefault("video_stream_url", None)

    seen_keys: set[tuple[str, str]] = set()
    for raw_key in payload.get("seen_keys") or []:
        if isinstance(raw_key, list) and len(raw_key) == 2:
            seen_keys.add((str(raw_key[0]), str(raw_key[1])))
    if not seen_keys:
        for entry in entries:
            key = checkpoint_seen_key_from_entry(entry)
            if key:
                seen_keys.add(key)

    logger.log(
        "[checkpoint] retomando checkpoint "
        f"total={len(entries)} status_counts={json.dumps(build_status_counts(entries), ensure_ascii=False)} "
        f"path={checkpoint_path}"
    )
    return entries, seen_keys


def write_outputs(
    *,
    output_dir: Path,
    build_dir: Path,
    output_stem: str,
    payload: dict[str, Any],
    entries: list[dict[str, Any]],
) -> tuple[Path, Path]:
    for entry in entries:
        entry.setdefault("video_stream_url", None)

    build_dir.mkdir(parents=True, exist_ok=False)
    json_build_path = build_dir / f"{output_stem}.json"
    tsv_build_path = build_dir / f"{output_stem}.tsv"

    json_build_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with tsv_build_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TSV_COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            writer.writerow({column: "" if entry.get(column) is None else entry.get(column) for column in TSV_COLUMNS})

    json_final_path = output_dir / f"{output_stem}.json"
    tsv_final_path = output_dir / f"{output_stem}.tsv"
    os.replace(json_build_path, json_final_path)
    os.replace(tsv_build_path, tsv_final_path)
    return json_final_path, tsv_final_path


def open_course_page_for_collection(page: Any, config: RuntimeConfig, logger: Logger, reason: str) -> None:
    logger.log(f"[legacy_flow.goto] motivo={reason} curso={config.course_url}")
    page.goto(config.course_url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms)
    if page_requires_login(page):
        if not config.email or not config.password:
            raise RuntimeError(
                "Sessao expirada ou ausente e credenciais nao configuradas. "
                "Revalide o storage_state ou informe --env-file com FEDERAL_ONLINE_EMAIL/PASSWORD."
            )
        logger.log("[auth] login requerido; executando relogin automatico.")
        login_if_needed(page, config, logger)
        logger.log(f"[legacy_flow.goto_after_login] reabrindo curso={config.course_url}")
        page.goto(config.course_url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=config.nav_timeout_ms)
    except PlaywrightTimeoutError:
        logger.log("[warn] load state nao estabilizou; seguindo com DOM atual.")
    neutralize_intro_overlay(page, logger)
    save_storage_state(page.context, config.storage_state_file)
    logger.log(f"storage_state persistido em: {config.storage_state_file}")

    try:
        page.locator("#idVideo").wait_for(state="attached", timeout=config.nav_timeout_ms)
    except PlaywrightTimeoutError:
        logger.log("[warn] #idVideo nao apareceu no carregamento inicial; seguindo pela arvore.")


def collect_index(
    args: argparse.Namespace,
    config: RuntimeConfig,
    logger: Logger,
    checkpoint_path: Path,
) -> list[dict[str, Any]]:
    if sync_playwright is None:
        raise RuntimeError(
            "Dependencia ausente: playwright. Instale com python3 -m pip install playwright "
            "e python3 -m playwright install chromium."
        )

    entries, seen_keys = load_checkpoint(
        checkpoint_path,
        course_url=config.course_url,
        path_filters=args.path_filter,
        logger=logger,
        resume_enabled=not args.no_resume,
    )
    captured_at = now_utc_iso()
    storage_state = choose_storage_state(args, config, logger)

    def checkpoint_writer() -> None:
        write_checkpoint(
            checkpoint_path,
            course_url=config.course_url,
            path_filters=args.path_filter,
            entries=entries,
            seen_keys=seen_keys,
            logger=logger,
        )

    browser = None
    playwright = None
    context = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=config.headless)
        context_kwargs: dict[str, Any] = {}
        if storage_state:
            context_kwargs["storage_state"] = str(storage_state)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(config.nav_timeout_ms)
        open_course_page_for_collection(page, config, logger, reason="inicio")

        if args.limit is not None and len(entries) >= args.limit:
            logger.log(
                f"[checkpoint] limite ja satisfeito pelo checkpoint total={len(entries)} limit={args.limit}"
            )
            return entries

        session_refreshes = 0
        while True:
            try:
                root = resolve_collection_root(page, config, logger)
                walk_cards(
                    page=page,
                    logger=logger,
                    container=root,
                    breadcrumbs=[],
                    path_filters=args.path_filter,
                    course_url=config.course_url,
                    captured_at=captured_at,
                    entries=entries,
                    seen_keys=seen_keys,
                    checkpoint_writer=checkpoint_writer,
                    limit=args.limit,
                )
                break
            except SessionExpiredDuringCollection as exc:
                session_refreshes += 1
                if session_refreshes > MAX_SESSION_REFRESHES:
                    raise RuntimeError(
                        f"Sessao expirou {session_refreshes} vezes durante a coleta; abortando."
                    ) from exc
                logger.log(
                    "[auth] sessao expirada no meio da coleta; "
                    f"tentativa={session_refreshes}/{MAX_SESSION_REFRESHES} detalhe={exc}"
                )
                open_course_page_for_collection(
                    page,
                    config,
                    logger,
                    reason=f"retomar-apos-expiracao-{session_refreshes}",
                )
                logger.log(
                    "[auth] coleta sera retomada do inicio da arvore; "
                    f"entradas_ja_coletadas={len(entries)}"
                )
        return entries
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


def main() -> int:
    args = parse_args()
    args.state_root = args.state_root.resolve()
    args.env_file = args.env_file.expanduser()
    if args.storage_state:
        args.storage_state = args.storage_state.expanduser()
    args.path_filter = clean_path_filters(args.path_filter)

    config = build_config(args)
    config.state_root.mkdir(parents=True, exist_ok=True)
    config.persistent_logs_dir.mkdir(parents=True, exist_ok=True)

    latest_log = config.persistent_logs_dir / "fo_sync_aulas.latest.log"
    latest_log.write_text("", encoding="utf-8")
    logger = Logger([latest_log])

    output_dir = config.state_root / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    filter_slug = path_filter_slug(args.path_filter)
    output_stem = "aulas_index" if not filter_slug else f"aulas_index.filtered_{filter_slug}"
    checkpoint_filename = CHECKPOINT_FILENAME if not filter_slug else f"checkpoint.filtered_{filter_slug}.json"
    checkpoint_path = output_dir / checkpoint_filename
    build_dir = output_dir / f"{BUILD_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"

    logger.log("Inicio da coleta semanal de metadados de aulas do Federal Online.")
    logger.log(f"course_url_source={'arg' if args.course_url else 'default'}")
    logger.log(f"course_url_effective={config.course_url}")
    logger.log(f"Curso alvo: {config.course_url}")
    logger.log(f"State root: {config.state_root}")
    logger.log(f"Diretorio final do indice: {output_dir}")
    if args.path_filter:
        logger.log(f"Path filters: {json.dumps(args.path_filter, ensure_ascii=False)}")
        logger.log(f"Output filtrado: {output_stem}.json / {output_stem}.tsv")
    logger.log(f"Checkpoint: {checkpoint_path}")
    logger.log(f"Log latest: {latest_log}")
    if not args.env_file.exists():
        logger.log(
            f"Env file nao encontrado em {args.env_file}; usando variaveis de ambiente "
            "ou storage_state existente."
        )

    try:
        cleanup_old_builds(output_dir, keep_dir=build_dir, age_hours=args.cleanup_age_hours, logger=logger)
        entries = collect_index(args, config, logger, checkpoint_path)
        if not entries and not args.allow_empty:
            raise RuntimeError("Nenhuma aula coletada; indice vazio nao sera promovido.")

        status_counts = dict(Counter(str(entry.get("status") or "") for entry in entries))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now_utc_iso(),
            "course_url": config.course_url,
            "path_filters": args.path_filter,
            "total": len(entries),
            "status_counts": status_counts,
            "aulas": entries,
        }
        json_path, tsv_path = write_outputs(
            output_dir=output_dir,
            build_dir=build_dir,
            output_stem=output_stem,
            payload=payload,
            entries=entries,
        )
        logger.log(f"Indice promovido: {json_path}")
        logger.log(f"Indice TSV promovido: {tsv_path}")
        logger.log(f"Resumo: total={len(entries)} status_counts={json.dumps(status_counts, ensure_ascii=False)}")
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            logger.log(f"[checkpoint] removido apos conclusao: {checkpoint_path}")
        try:
            build_dir.rmdir()
        except OSError as exc:
            logger.log(f"[warn] build temporario vazio nao removido: {summarize_exception(exc)}")
        cleanup_old_builds(output_dir, keep_dir=None, age_hours=args.cleanup_age_hours, logger=logger)
        return 0
    except Exception as exc:
        logger.log(f"Coleta falhou: {summarize_exception(exc)}")
        logger.log(f"Build temporario preservado para diagnostico: {build_dir}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
