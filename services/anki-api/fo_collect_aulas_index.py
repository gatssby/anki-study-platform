#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "federal_online"
LEGACY_SESSION_FILE = PROJECT_ROOT / "work" / "fo_bridge" / "session" / "fo_storage_state.json"
DEFAULT_COURSE_URL = (
    "https://www.federalonline.com.br/portal/curso-aula/"
    "produto-pacote/57b0cfa5139851ce785e14e9bd584626/extensivo-ufpr-2026"
)

OUTPUT_SUBDIR = "cronograma_fo"
BUILD_PREFIX = ".build_aulas_index_"
SCHEMA_VERSION = 1

TSV_COLUMNS = [
    "area",
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
)

OPERATIONAL_ITEM_KEYWORDS = (
    "bem-vindo ao federal online",
    "comece por aqui",
    "como enviar redacoes",
    "como digitalizar redacoes",
)


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
        default=DEFAULT_STATE_ROOT,
        help=f"Diretorio de estado. Padrao: {DEFAULT_STATE_ROOT}",
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
        "--allow-empty",
        action="store_true",
        help="Permite promover indice vazio. Por padrao, indice vazio falha.",
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
    parts = [part.strip() for part in subject_group.split(">") if part.strip()]
    if not parts:
        return "", ""
    return parts[0], parts[-1]


def is_academic_source_ref(subject_group: str) -> bool:
    area, _ = split_source_ref(subject_group)
    allowed_roots = {normalize_key(area_name) for area_name in ACADEMIC_ROOT_AREAS}
    return normalize_key(area) in allowed_roots


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
    text = normalize_key(
        " ".join(
            value
            for value in (
                item_info.get("title"),
                item_info.get("clickedText"),
                item_info.get("rawText"),
            )
            if value
        )
    )
    detail = summarize_exception(exc)
    if any(marker in text for marker in ("em atualizacao", "aguardando", "nao lancada")):
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
) -> dict[str, Any]:
    area, disciplina = split_source_ref(subject_group)
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
    limit: int | None,
) -> bool:
    item_paths = list_logical_descendant_paths(
        container=body,
        target_selector=".body-list li",
        blocked_ancestor_selector=".card",
    )

    if not item_paths:
        return False

    if not is_academic_source_ref(subject_group):
        logger.log(f"[skip] bloco fora do indice academico source_ref={subject_group}")
        return False

    video_order = 0
    for item_path in item_paths:
        item = resolve_descendant_path(body, item_path)
        item_info = extract_item_info(item)

        if item_info.get("mediaType") != "V":
            continue
        if is_ignored_item(item_info):
            continue
        if is_operational_item(item_info):
            skipped_title = normalize_sidebar_title(
                item_info.get("title") or item_info.get("clickedText")
            )
            logger.log(f"[skip] item operacional source_ref={subject_group} item={skipped_title}")
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
        seen_keys.add(dedupe_key)

        previous_state = read_current_video(page)
        item.scroll_into_view_if_needed()
        try:
            item.click()
            current = wait_for_selected_video(
                page=page,
                previous_state=previous_state,
                expected_id=normalize_space(item_info.get("dataVideo")) or None,
                expected_title=title_hint or None,
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
            )
            entries.append(entry)
            logger.log(
                "[write] "
                f"status={entry['status']} source_ref={subject_group} ordem={video_order} "
                f"media_id={entry['media_id'] or '-'} titulo={entry['titulo_original']}"
            )
        except Exception as exc:
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
            )
            entries.append(entry)
            logger.log(
                "[warn] "
                f"status={status} source_ref={subject_group} ordem={video_order} "
                f"titulo={entry['titulo_original'] or title_hint or '-'} detalhe={error}"
            )

        if limit is not None and len(entries) >= limit:
            return True

    return False


def walk_cards(
    *,
    page: Any,
    logger: Logger,
    container: Any,
    breadcrumbs: list[str],
    course_url: str,
    captured_at: str,
    entries: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    limit: int | None,
) -> bool:
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

        try:
            body = expand_card(page, logger, card, path_label)
        except Exception as exc:
            logger.log(f"[warn] bloco ignorado path={path_label} detalhe={summarize_exception(exc)}")
            continue

        should_stop = collect_direct_videos(
            page=page,
            logger=logger,
            body=body,
            subject_group=path_label,
            course_url=course_url,
            captured_at=captured_at,
            entries=entries,
            seen_keys=seen_keys,
            limit=limit,
        )
        if should_stop:
            return True

        should_stop = walk_cards(
            page=page,
            logger=logger,
            container=body,
            breadcrumbs=current_path,
            course_url=course_url,
            captured_at=captured_at,
            entries=entries,
            seen_keys=seen_keys,
            limit=limit,
        )
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


def write_outputs(
    *,
    output_dir: Path,
    build_dir: Path,
    payload: dict[str, Any],
    entries: list[dict[str, Any]],
) -> tuple[Path, Path]:
    build_dir.mkdir(parents=True, exist_ok=False)
    json_build_path = build_dir / "aulas_index.json"
    tsv_build_path = build_dir / "aulas_index.tsv"

    json_build_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with tsv_build_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TSV_COLUMNS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            writer.writerow({column: "" if entry.get(column) is None else entry.get(column) for column in TSV_COLUMNS})

    json_final_path = output_dir / "aulas_index.json"
    tsv_final_path = output_dir / "aulas_index.tsv"
    os.replace(json_build_path, json_final_path)
    os.replace(tsv_build_path, tsv_final_path)
    return json_final_path, tsv_final_path


def collect_index(args: argparse.Namespace, config: RuntimeConfig, logger: Logger) -> list[dict[str, Any]]:
    if sync_playwright is None:
        raise RuntimeError(
            "Dependencia ausente: playwright. Instale com python3 -m pip install playwright "
            "e python3 -m playwright install chromium."
        )

    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    captured_at = now_utc_iso()
    storage_state = choose_storage_state(args, config, logger)

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
        logger.log(f"[legacy_flow.goto] abrindo curso={config.course_url}")
        page.goto(config.course_url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms)
        if page_requires_login(page):
            if not config.email or not config.password:
                raise RuntimeError(
                    "Sessao expirada ou ausente e credenciais nao configuradas. "
                    "Revalide o storage_state ou informe --env-file com FEDERAL_ONLINE_EMAIL/PASSWORD."
                )
            login_if_needed(page, config, logger)
            logger.log(f"[legacy_flow.goto_after_login] reabrindo curso={config.course_url}")
            page.goto(config.course_url, wait_until="domcontentloaded", timeout=config.nav_timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=config.nav_timeout_ms)
        except PlaywrightTimeoutError:
            logger.log("[warn] load state nao estabilizou; seguindo com DOM atual.")
        neutralize_intro_overlay(page, logger)
        context.storage_state(path=str(config.storage_state_file))
        logger.log(f"storage_state persistido em: {config.storage_state_file}")

        try:
            page.locator("#idVideo").wait_for(state="attached", timeout=config.nav_timeout_ms)
        except PlaywrightTimeoutError:
            logger.log("[warn] #idVideo nao apareceu no carregamento inicial; seguindo pela arvore.")

        root = resolve_collection_root(page, config, logger)
        walk_cards(
            page=page,
            logger=logger,
            container=root,
            breadcrumbs=[],
            course_url=config.course_url,
            captured_at=captured_at,
            entries=entries,
            seen_keys=seen_keys,
            limit=args.limit,
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

    config = build_config(args)
    config.state_root.mkdir(parents=True, exist_ok=True)
    config.persistent_logs_dir.mkdir(parents=True, exist_ok=True)

    latest_log = config.persistent_logs_dir / "fo_sync_aulas.latest.log"
    latest_log.write_text("", encoding="utf-8")
    logger = Logger([latest_log])

    output_dir = config.state_root / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir = output_dir / f"{BUILD_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"

    logger.log("Inicio da coleta semanal de metadados de aulas do Federal Online.")
    logger.log(f"course_url_source={'arg' if args.course_url else 'default'}")
    logger.log(f"course_url_effective={config.course_url}")
    logger.log(f"Curso alvo: {config.course_url}")
    logger.log(f"State root: {config.state_root}")
    logger.log(f"Diretorio final do indice: {output_dir}")
    logger.log(f"Log latest: {latest_log}")
    if not args.env_file.exists():
        logger.log(
            f"Env file nao encontrado em {args.env_file}; usando variaveis de ambiente "
            "ou storage_state existente."
        )

    try:
        cleanup_old_builds(output_dir, keep_dir=build_dir, age_hours=args.cleanup_age_hours, logger=logger)
        entries = collect_index(args, config, logger)
        if not entries and not args.allow_empty:
            raise RuntimeError("Nenhuma aula coletada; indice vazio nao sera promovido.")

        status_counts = dict(Counter(str(entry.get("status") or "") for entry in entries))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now_utc_iso(),
            "course_url": config.course_url,
            "total": len(entries),
            "status_counts": status_counts,
            "aulas": entries,
        }
        json_path, tsv_path = write_outputs(
            output_dir=output_dir,
            build_dir=build_dir,
            payload=payload,
            entries=entries,
        )
        logger.log(f"Indice promovido: {json_path}")
        logger.log(f"Indice TSV promovido: {tsv_path}")
        logger.log(f"Resumo: total={len(entries)} status_counts={json.dumps(status_counts, ensure_ascii=False)}")
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
