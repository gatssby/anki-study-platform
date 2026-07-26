#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from fo_session_security import protect_storage_state, save_storage_state
from fo_sync_materials import (
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_ENV_FILE,
    DEFAULT_NAV_TIMEOUT_MS,
    DEFAULT_STATE_ROOT,
    Logger,
    ManifestEntry,
    capture_pdf_bytes_from_url,
    extract_pdf_urls,
    is_pdf_file,
    logged_step,
    login_if_needed,
    material_duplicate_conflicts,
    neutralize_intro_overlay,
    normalize_text,
    now_utc_iso,
    page_requires_login,
    parse_env_file,
    sha256_file,
    summarize_exception,
    write_manifest,
)

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - handled at runtime on the VPS.
    PlaywrightTimeoutError = RuntimeError  # type: ignore[assignment]
    sync_playwright = None


MATERIAIS_URL = "https://www.federalonline.com.br/portal/materiais"
TARGET_COURSE_LABEL = "Extensivo UFPR 2026"
TARGET_COURSE_VALUE = "P-319"
ONEDRIVE_REMOTE = "onedrive:Federal Online PDFs"

PORTAL_SUBJECTS = [
    "Biologia - 2ª Fase",
    "Biologia - ENEM",
    "Biologia I - Federal",
    "Biologia II - Federal",
    "Biologia III - Federal",
    "Filosofia - Federal",
    "Física I - Federal",
    "Física II - Federal",
    "Física III - Federal",
    "Geografia I - Federal",
    "Geografia II - Federal",
    "Geografia III - Federal",
    "História I - Federal",
    "História II - Federal",
    "Literatura - Federal",
    "Matemática I - Federal",
    "Matemática II - Federal",
    "Matemática III - Federal",
    "Matemática IV - Federal",
    "Português I - Federal",
    "Português II - Federal",
    "Química - 2ª Fase",
    "Química I - Federal",
    "Química II - Federal",
    "Química III - Federal",
    "Sociologia - Federal",
]

OUTPUT_SUBJECT_OVERRIDES = {
    "Filosofia - Federal": "Filosofia Obras",
    "Sociologia - Federal": "Sociologia Obras",
    "Literatura - Federal": "Literatura Obras",
}

SUBJECT_AREAS = {
    "Biologia": [
        "Biologia - 2ª Fase",
        "Biologia - ENEM",
        "Biologia I - Federal",
        "Biologia II - Federal",
        "Biologia III - Federal",
    ],
    "Filosofia": [
        "Filosofia - Federal",
    ],
    "Física": [
        "Física I - Federal",
        "Física II - Federal",
        "Física III - Federal",
    ],
    "Geografia": [
        "Geografia I - Federal",
        "Geografia II - Federal",
        "Geografia III - Federal",
    ],
    "História": [
        "História I - Federal",
        "História II - Federal",
    ],
    "Literatura": [
        "Literatura - Federal",
    ],
    "Matemática": [
        "Matemática I - Federal",
        "Matemática II - Federal",
        "Matemática III - Federal",
        "Matemática IV - Federal",
    ],
    "Português": [
        "Português I - Federal",
        "Português II - Federal",
    ],
    "Química": [
        "Química - 2ª Fase",
        "Química I - Federal",
        "Química II - Federal",
        "Química III - Federal",
    ],
    "Sociologia": [
        "Sociologia - Federal",
    ],
}

CANONICAL_TYPES = [
    "Lista de Exercícios",
    "Gabarito Comentado",
    "Resumo",
]

TYPE_ALIASES = {
    normalize_text("Lista de Exercícios"): "Lista de Exercícios",
    normalize_text("Lista de exercícios"): "Lista de Exercícios",
    normalize_text("Gabarito Comentado"): "Gabarito Comentado",
    normalize_text("Gabarito comentado"): "Gabarito Comentado",
    normalize_text("Resumo"): "Resumo",
}


@dataclass(frozen=True)
class PortalRuntimeConfig:
    env_file: Path
    state_root: Path
    storage_state_file: Path
    final_dir: Path
    persistent_logs_dir: Path
    persistent_log_file: Path
    materials_url: str
    login_url: str | None
    email: str
    password: str
    headless: bool
    nav_timeout_ms: int
    download_timeout_ms: int


@dataclass(frozen=True)
class PortalTarget:
    group: str
    portal_subject: str
    output_subject: str
    material_type: str

    @property
    def target_key(self) -> str:
        return f"{self.portal_subject} | {self.material_type or '*'}"

    @property
    def filename(self) -> str:
        label = self.material_type or self.output_subject
        if label.lower().endswith(".pdf"):
            label = label[:-4].strip()
        return sanitize_filename(f"{self.portal_subject}__{label}.pdf")


@dataclass(frozen=True)
class PortalRow:
    position: int
    row_id: str
    row_text: str
    title_text: str
    subject_text: str
    material_type: str
    raw_date: str
    action_id: str
    action_text: str
    action_href: str
    action_tag: str
    action_score: int
    data_path: str
    data_token: str
    data_value: str


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", name)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "material.pdf"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sincroniza materiais PDF do Federal Online via /portal/materiais."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Arquivo env fora do repositorio. Padrao: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=f"Diretorio de saida/estado. Padrao: {DEFAULT_STATE_ROOT}",
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
        "--download-timeout-ms",
        type=int,
        default=DEFAULT_DOWNLOAD_TIMEOUT_MS,
        help=f"Timeout de download em ms. Padrao: {DEFAULT_DOWNLOAD_TIMEOUT_MS}",
    )
    parser.add_argument(
        "--only-subject",
        default="",
        help="Executa apenas um subject especifico, ex: 'Física I - Federal'.",
    )
    parser.add_argument(
        "--only-type",
        default="",
        help="Filtro legado por substring no titulo do material, ex: 'Resumo'.",
    )
    parser.add_argument(
        "--sync-onedrive",
        action="store_true",
        help=f"Executa rclone sync para {ONEDRIVE_REMOTE} ao final de uma execucao bem-sucedida.",
    )
    return parser.parse_args()


def output_subject_for(portal_subject: str) -> str:
    if portal_subject in OUTPUT_SUBJECT_OVERRIDES:
        return OUTPUT_SUBJECT_OVERRIDES[portal_subject]
    if portal_subject.endswith(" - Federal"):
        return portal_subject[: -len(" - Federal")].strip()
    return portal_subject


def build_targets(material_filter: str = "") -> list[PortalTarget]:
    targets: list[PortalTarget] = []
    for portal_subject in PORTAL_SUBJECTS:
        output_subject = output_subject_for(portal_subject)
        group = "obra" if output_subject.endswith("Obras") else "disciplina"
        targets.append(
            PortalTarget(
                group=group,
                portal_subject=portal_subject,
                output_subject=output_subject,
                material_type=material_filter.strip(),
            )
        )
    return targets


def build_runtime_config(args: argparse.Namespace) -> PortalRuntimeConfig:
    env_values = parse_env_file(args.env_file)
    missing = [
        key
        for key in (
            "FEDERAL_ONLINE_EMAIL",
            "FEDERAL_ONLINE_PASSWORD",
        )
        if not env_values.get(key)
    ]
    if missing:
        raise ValueError(
            "Variaveis obrigatorias ausentes em "
            f"{args.env_file}: {', '.join(missing)}"
        )

    state_root = args.state_root
    final_dir = state_root / "materiais_fo"
    persistent_logs_dir = state_root / "logs"
    return PortalRuntimeConfig(
        env_file=args.env_file,
        state_root=state_root,
        storage_state_file=state_root / "storage_state.json",
        final_dir=final_dir,
        persistent_logs_dir=persistent_logs_dir,
        persistent_log_file=persistent_logs_dir / "fo_sync_materials_from_portal_materiais.latest.log",
        materials_url=env_values.get("FEDERAL_ONLINE_MATERIAIS_URL") or MATERIAIS_URL,
        login_url=env_values.get("FEDERAL_ONLINE_LOGIN_URL"),
        email=env_values["FEDERAL_ONLINE_EMAIL"],
        password=env_values["FEDERAL_ONLINE_PASSWORD"],
        headless=not args.headed,
        nav_timeout_ms=args.nav_timeout_ms,
        download_timeout_ms=args.download_timeout_ms,
    )


def canonical_material_type(text: str) -> str | None:
    normalized = normalize_text(text)
    for alias, canonical in TYPE_ALIASES.items():
        if alias and alias in normalized:
            return canonical
    return None


def parse_portal_date(raw_date: str) -> datetime | None:
    match = re.search(r"\b(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?\b", raw_date or "")
    if not match:
        return None
    value = match.group(1)
    hour = match.group(2)
    try:
        if hour:
            return datetime.strptime(f"{value} {hour}", "%d/%m/%Y %H:%M")
        return datetime.strptime(value, "%d/%m/%Y")
    except ValueError:
        return None


def storage_title_from_portal_title(title_text: str, fallback: str) -> str:
    title = re.sub(
        r"\s+\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?\s*$",
        "",
        title_text or "",
    ).strip()
    title = re.sub(r"\.pdf\s*$", "", title, flags=re.IGNORECASE).strip()
    return title or fallback


def clean_subject_label(portal_subject: str) -> str:
    if portal_subject.endswith(" - Federal"):
        return portal_subject[: -len(" - Federal")].strip()
    return portal_subject.strip()


def filename_for_row(portal_subject: str, storage_title: str) -> str:
    canonical_type = canonical_material_type(storage_title)
    if canonical_type:
        return sanitize_filename(f"{clean_subject_label(portal_subject)} - {canonical_type}.pdf")
    return sanitize_filename(f"{portal_subject}__{storage_title}.pdf")


def unique_destination_for_filename(target_dir: Path, filename: str) -> Path:
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate

    base = Path(filename).stem
    suffix = Path(filename).suffix or ".pdf"
    index = 2
    while True:
        candidate = target_dir / sanitize_filename(f"{base} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def subject_area_for(portal_subject: str) -> str:
    for area, subjects in SUBJECT_AREAS.items():
        if portal_subject in subjects:
            return area
    return "Outros"


def cleanup_stale_build_dirs(state_root: Path, logger: Logger, keep: set[Path] | None = None) -> None:
    keep = {path.resolve() for path in (keep or set())}
    patterns = (".raw_build_*", ".build_materiais_fo_*", ".raw_previous", ".materiais_fo_previous")
    for pattern in patterns:
        for path in state_root.glob(pattern):
            resolved = path.resolve()
            if resolved in keep:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                logger.log(f"limpeza_build_legado removido={path}")


def promote_output_dir(state_root: Path, temp_build_dir: Path, final_dir: Path) -> None:
    backup_dir = state_root / ".materiais_fo_previous"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if final_dir.exists():
        final_dir.rename(backup_dir)
    temp_build_dir.rename(final_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


def sync_to_onedrive(source_dir: Path, logger: Logger) -> None:
    command = ["rclone", "sync", str(source_dir), ONEDRIVE_REMOTE]
    logger.log(f"onedrive_sync.inicio comando={' '.join(command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("rclone nao encontrado no ambiente.") from exc
    if result.stdout.strip():
        logger.log(f"onedrive_sync.stdout={result.stdout.strip()[:4000]}")
    if result.stderr.strip():
        logger.log(f"onedrive_sync.stderr={result.stderr.strip()[:4000]}")
    if result.returncode != 0:
        raise RuntimeError(f"rclone sync falhou com exit code {result.returncode}.")
    logger.log("onedrive_sync.ok")


def debug_visible_controls(page: Any) -> list[dict[str, str]]:
    return page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const out = [];
          for (const el of document.querySelectorAll("select, button, [role='combobox'], .select2-selection, .choices__inner")) {
            if (!visible(el)) continue;
            out.push({
              tag: (el.tagName || "").toLowerCase(),
              text: normalize(el.innerText || el.textContent || ""),
              name: el.getAttribute("name") || "",
              id: el.id || "",
              classes: el.className || "",
            });
            if (out.length >= 30) break;
          }
          return out;
        }
        """
    )


def open_authenticated_materials_page(context: Any, config: PortalRuntimeConfig, logger: Logger) -> Any:
    page = context.new_page()
    page.set_default_timeout(config.nav_timeout_ms)
    logged_step(
        logger,
        "open_authenticated_materials_page.goto",
        config.materials_url,
        config.nav_timeout_ms,
        lambda: page.goto(
            config.materials_url,
            wait_until="domcontentloaded",
            timeout=config.nav_timeout_ms,
        ),
    )

    if page_requires_login(page):
        if config.login_url:
            logger.log("Acesso direto caiu no login; abrindo login_url configurada.")
            logged_step(
                logger,
                "open_authenticated_materials_page.goto_login",
                config.login_url,
                config.nav_timeout_ms,
                lambda: page.goto(
                    config.login_url,
                    wait_until="domcontentloaded",
                    timeout=config.nav_timeout_ms,
                ),
            )
        login_if_needed(page, config, logger)
        logged_step(
            logger,
            "open_authenticated_materials_page.goto_after_login",
            config.materials_url,
            config.nav_timeout_ms,
            lambda: page.goto(
                config.materials_url,
                wait_until="domcontentloaded",
                timeout=config.nav_timeout_ms,
            ),
        )
    else:
        logger.log("Acesso direto ao portal de materiais funcionou com a sessao atual.")

    logged_step(
        logger,
        "open_authenticated_materials_page.wait_for_load_state",
        "load-no-portal-materiais",
        config.nav_timeout_ms,
        lambda: page.wait_for_load_state("load", timeout=config.nav_timeout_ms),
    )
    try:
        logged_step(
            logger,
            "open_authenticated_materials_page.wait_for_ready",
            "select|table|button",
            config.nav_timeout_ms,
            lambda: page.wait_for_function(
                """
                () => {
                  return (
                    document.querySelectorAll("select").length > 0 ||
                    document.querySelectorAll("table").length > 0 ||
                    document.querySelectorAll("button").length > 0
                  );
                }
                """,
                timeout=config.nav_timeout_ms,
            ),
        )
    except Exception as exc:
        logger.log(
            "Portal de materiais nao estabilizou por seletor concreto; seguindo com delay defensivo. "
            f"detalhe={summarize_exception(exc)}"
        )
        page.wait_for_timeout(1_000)

    neutralize_intro_overlay(page, logger)
    return page


def tag_filter_control(
    page: Any,
    label_keywords: list[str],
    fallback_index: int,
) -> dict[str, str] | None:
    return page.evaluate(
        """
        ({ labelKeywords, fallbackIndex }) => {
          const normalize = (value) => (value || "")
            .normalize("NFKD")
            .replace(/[\\u0300-\\u036f]/g, "")
            .toLowerCase()
            .replace(/\\s+/g, " ")
            .trim();
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const keywords = (labelKeywords || []).map((item) => normalize(item));
          const scoreFor = (metaText, position) => {
            let score = 0;
            for (const keyword of keywords) {
              if (!keyword) continue;
              if (metaText.includes(keyword)) score += 100;
            }
            if (position === fallbackIndex) score += 10;
            return score;
          };
          const metaFor = (el) => {
            const chunks = [];
            const push = (value) => {
              const normalized = normalize(value);
              if (normalized) chunks.push(normalized);
            };
            push(el.getAttribute("name"));
            push(el.getAttribute("aria-label"));
            push(el.getAttribute("placeholder"));
            push(el.getAttribute("title"));
            push(el.id);
            if (el.id && window.CSS && CSS.escape) {
              const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (label) push(label.innerText || label.textContent || "");
            }
            const closestLabel = el.closest("label");
            if (closestLabel) push(closestLabel.innerText || closestLabel.textContent || "");
            const container = el.closest(".form-group, .form-floating, .field, .filters, .card, .card-body, form, .row, .col");
            if (container) {
              for (const label of container.querySelectorAll("label, .form-label, .label")) {
                push(label.innerText || label.textContent || "");
              }
            }
            if (el.previousElementSibling) {
              push(el.previousElementSibling.innerText || el.previousElementSibling.textContent || "");
            }
            return chunks.join(" | ");
          };

          const pick = (elements, kind) => {
            const candidates = [];
            let position = 0;
            for (const el of elements) {
              if (!visible(el)) continue;
              const metaText = metaFor(el);
              const score = scoreFor(metaText, position);
              const stamp = `${kind}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
              el.setAttribute("data-fo-filter-id", stamp);
              candidates.push({
                kind,
                filter_id: stamp,
                meta_text: metaText,
                score,
                position,
              });
              position += 1;
            }
            if (!candidates.length) return null;
            candidates.sort((a, b) => b.score - a.score || a.position - b.position);
            return candidates[0];
          };

          const native = pick(document.querySelectorAll("select"), "select");
          if (native) return native;

          const customSelectors = [
            "[role='combobox']",
            "[aria-haspopup='listbox']",
            ".select2-selection",
            ".choices__inner",
            ".dropdown-toggle"
          ];
          return pick(document.querySelectorAll(customSelectors.join(",")), "custom");
        }
        """,
        {
            "labelKeywords": label_keywords,
            "fallbackIndex": fallback_index,
        },
    )


def set_native_select_value(page: Any, filter_id: str, value: str) -> None:
    locator = page.locator(f"[data-fo-filter-id='{filter_id}']").first
    try:
        locator.select_option(label=value, timeout=5_000)
        return
    except Exception:
        pass

    page.evaluate(
        """
        ({ filterId, targetValue }) => {
          const normalize = (value) => (value || "")
            .normalize("NFKD")
            .replace(/[\\u0300-\\u036f]/g, "")
            .toLowerCase()
            .replace(/\\s+/g, " ")
            .trim();
          const select = document.querySelector(`[data-fo-filter-id="${filterId}"]`);
          if (!select) throw new Error("select nao encontrado");
          const targetNorm = normalize(targetValue);
          let matchedValue = "";
          for (const option of select.options) {
            const labelNorm = normalize(option.label || option.textContent || "");
            const valueNorm = normalize(option.value || "");
            if (labelNorm === targetNorm || labelNorm.includes(targetNorm) || valueNorm === targetNorm) {
              matchedValue = option.value;
              break;
            }
          }
          if (!matchedValue) {
            throw new Error(`opcao nao encontrada: ${targetValue}`);
          }
          select.value = matchedValue;
          select.dispatchEvent(new Event("input", { bubbles: true }));
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        """,
        {"filterId": filter_id, "targetValue": value},
    )


def click_visible_option_by_text(page: Any, value: str) -> None:
    option_selectors = [
        "[role='option']",
        ".select2-results__option",
        ".dropdown-item",
        ".choices__item--choice",
        ".active-result",
        "li",
    ]
    target_norm = normalize_text(value)
    for selector in option_selectors:
        locator = page.locator(selector).filter(has_text=re.compile(re.escape(value), re.I)).first
        try:
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=5_000)
                return
        except Exception:
            continue

    page.evaluate(
        """
        ({ targetValue }) => {
          const normalize = (value) => (value || "")
            .normalize("NFKD")
            .replace(/[\\u0300-\\u036f]/g, "")
            .toLowerCase()
            .replace(/\\s+/g, " ")
            .trim();
          const targetNorm = normalize(targetValue);
          const selectors = [
            "[role='option']",
            ".select2-results__option",
            ".dropdown-item",
            ".choices__item--choice",
            ".active-result",
            "li"
          ];
          for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
              const style = window.getComputedStyle(el);
              const rect = el.getBoundingClientRect();
              if (style.visibility === "hidden" || style.display === "none" || rect.width <= 0 || rect.height <= 0) continue;
              const text = normalize(el.innerText || el.textContent || "");
              if (!text) continue;
              if (text === targetNorm || text.includes(targetNorm)) {
                el.click();
                return;
              }
            }
          }
          throw new Error(`opcao visivel nao encontrada: ${targetValue}`);
        }
        """,
        {"targetValue": value},
    )


def set_custom_filter_value(page: Any, filter_id: str, value: str) -> None:
    locator = page.locator(f"[data-fo-filter-id='{filter_id}']").first
    locator.click(timeout=5_000)
    page.wait_for_timeout(300)

    search_selectors = [
        "input[type='search']",
        ".select2-search__field",
        "[role='combobox'] input",
        "input[aria-autocomplete='list']",
    ]
    for selector in search_selectors:
        search = page.locator(selector).first
        try:
            if search.count() > 0 and search.is_visible():
                search.fill(value, timeout=5_000)
                page.wait_for_timeout(300)
                break
        except Exception:
            continue

    click_visible_option_by_text(page, value)


def set_filter_value(
    page: Any,
    logger: Logger,
    label_keywords: list[str],
    fallback_index: int,
    value: str,
) -> dict[str, str]:
    tagged = tag_filter_control(page, label_keywords, fallback_index)
    if not tagged:
        controls = debug_visible_controls(page)
        raise RuntimeError(
            "Nao foi possivel localizar controle de filtro. "
            f"keywords={label_keywords} controles={json.dumps(controls, ensure_ascii=False)}"
        )

    logger.log(
        f"[set_filter_value] kind={tagged['kind']} keywords={label_keywords} "
        f"meta={tagged.get('meta_text', '')[:200]} value={value}"
    )

    if tagged["kind"] == "select":
        set_native_select_value(page, tagged["filter_id"], value)
    else:
        set_custom_filter_value(page, tagged["filter_id"], value)

    page.wait_for_timeout(700)
    neutralize_intro_overlay(page, logger)
    applied_value = get_filter_display_value(page, tagged["filter_id"])
    logger.log(
        f"[set_filter_value] valor_aplicado keywords={label_keywords} "
        f"value={applied_value[:200]}"
    )
    return {
        "filter_id": str(tagged["filter_id"]),
        "kind": str(tagged["kind"]),
        "applied_value": applied_value,
    }


def set_filter_value_by_selector(
    page: Any,
    logger: Logger,
    selector: str,
    value: str,
    *,
    by: str = "label",
) -> dict[str, str]:
    locator = page.locator(selector).first
    if locator.count() <= 0:
        raise RuntimeError(f"Filtro nao encontrado: {selector}")
    if by == "value":
        locator.select_option(value=value, timeout=5_000)
    else:
        locator.select_option(label=value, timeout=5_000)
    page.wait_for_timeout(500)
    neutralize_intro_overlay(page, logger)
    selected_info = get_selected_select_info(page, selector)
    logger.log(
        f"[set_filter_value_by_selector] selector={selector} by={by} "
        f"value_aplicado={selected_info['value'][:120]} text_resultante={selected_info['text'][:200]}"
    )
    return {
        "filter_id": selector,
        "kind": "select",
        "applied_value": selected_info["value"],
        "applied_text": selected_info["text"],
    }


def get_selected_select_info(page: Any, selector: str) -> dict[str, str]:
    return page.evaluate(
        """
        ({ selector }) => {
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const select = document.querySelector(selector);
          if (!select) return { value: "", text: "" };
          const selected = select.options[select.selectedIndex];
          return {
            value: normalize(select.value || ""),
            text: normalize(selected ? (selected.label || selected.textContent || "") : ""),
          };
        }
        """,
        {"selector": selector},
    )


def count_select_options(page: Any, selector: str) -> int:
    return int(
        page.evaluate(
            """
            ({ selector }) => {
              const select = document.querySelector(selector);
              return select ? select.options.length : 0;
            }
            """,
            {"selector": selector},
        )
    )


def wait_for_select_options_refresh(
    page: Any,
    config: PortalRuntimeConfig,
    logger: Logger,
    selector: str,
    previous_count: int,
) -> int:
    try:
        page.wait_for_function(
            """
            ({ selector, previousCount }) => {
              const select = document.querySelector(selector);
              if (!select) return false;
              const count = select.options.length;
              return count > 1 && count !== previousCount;
            }
            """,
            arg={"selector": selector, "previousCount": previous_count},
            timeout=config.nav_timeout_ms,
        )
    except Exception as exc:
        logger.log(
            f"[wait_for_select_options_refresh] selector={selector} sem mudanca detectada; "
            f"seguindo com delay defensivo detalhe={summarize_exception(exc)}"
        )
        page.wait_for_timeout(1_500)
    return count_select_options(page, selector)


def get_filter_display_value(page: Any, filter_id: str) -> str:
    return str(
        page.evaluate(
            """
            ({ filterId }) => {
              const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const el = document.querySelector(`[data-fo-filter-id="${filterId}"]`);
              if (!el) return "";
              const tag = (el.tagName || "").toLowerCase();
              if (tag === "select") {
                const selected = el.options[el.selectedIndex];
                return normalize(selected ? (selected.label || selected.textContent || selected.value || "") : el.value || "");
              }
              return normalize(
                el.innerText ||
                el.textContent ||
                el.getAttribute("title") ||
                el.getAttribute("aria-label") ||
                ""
              );
            }
            """,
            {"filterId": filter_id},
        )
    )


def count_visible_result_rows(page: Any) -> int:
    return int(
        page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
              };
              const rows = Array.from(document.querySelectorAll(".material-list-content table tbody tr")).filter((el) => {
                if (!visible(el)) return false;
                const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
                return !!text && text.length <= 2000;
              });
              return rows.length;
            }
            """
        )
    )


def click_search_button(page: Any, logger: Logger, portal_subject: str) -> None:
    clicked = page.evaluate(
        """
        () => {
          const button = document.querySelector("button#filterDocuments.btn.btn-block.btn-form[type='submit']");
          const form = document.querySelector("form#documentForm");
          if (!button || !form) {
            return { ok: false, reason: "search_button_or_form_not_found" };
          }
          const text = (button.innerText || button.textContent || button.getAttribute("value") || "").replace(/\\s+/g, " ").trim();
          button.click();
          return { ok: true, text, classes: button.className || "", form_id: form.id || "" };
        }
        """
    )
    if not clicked.get("ok"):
        raise RuntimeError(f"[{portal_subject}] Botao/formulario de busca nao encontrado.")
    logger.log(
        f"[{portal_subject}] clique_busca ok texto={clicked.get('text', '')!s} "
        f"classes={str(clicked.get('classes', ''))[:200]} form_id={clicked.get('form_id', '')}"
    )


def wait_for_materials_results(
    page: Any,
    config: PortalRuntimeConfig,
    logger: Logger,
    previous_count: int | None = None,
) -> None:
    try:
        page.wait_for_function(
            """
            ({ previousCount }) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
              };
              const rows = Array.from(document.querySelectorAll(".material-list-content table tbody tr")).filter((el) => {
                if (!visible(el)) return false;
                const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
                return !!text && text.length <= 2000;
              });
              if (rows.length > 0) {
                if (previousCount == null) return true;
                return rows.length !== previousCount || rows.some((row) => /\\b\\d{2}\\/\\d{2}\\/\\d{4}\\b/.test(row.innerText || row.textContent || ""));
              }
              const bodyText = (document.body?.innerText || "").toLowerCase();
              return bodyText.includes("nenhum") || bodyText.includes("nao encontrado");
            }
            """,
            arg={"previousCount": previous_count},
            timeout=config.nav_timeout_ms,
        )
    except Exception as exc:
        logger.log(
            "Tabela/lista de materiais nao estabilizou por wait_for_function; seguindo com delay defensivo. "
            f"detalhe={summarize_exception(exc)}"
        )
        page.wait_for_timeout(1_500)


def collect_visible_filter_diagnostics(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        ({ subjects }) => {
          const normalize = (value) => (value || "")
            .normalize("NFKD")
            .replace(/[\\u0300-\\u036f]/g, "")
            .toLowerCase()
            .replace(/\\s+/g, " ")
            .trim();
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const normalizedSubjects = (subjects || []).map((item) => ({
            raw: item,
            norm: normalize(item),
          }));
          const rows = Array.from(document.querySelectorAll(".material-list-content table tbody tr")).filter((el) => {
            if (!visible(el)) return false;
            const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
            return !!text && text.length <= 2000;
          });

          const visibleSubjects = new Set();
          const snippets = [];
          for (const row of rows) {
            const text = (row.innerText || row.textContent || "").replace(/\\s+/g, " ").trim();
            const textNorm = normalize(text);
            snippets.push(text.slice(0, 300));
            for (const subject of normalizedSubjects) {
              if (subject.norm && textNorm.includes(subject.norm)) {
                visibleSubjects.add(subject.raw);
              }
            }
          }

          const getSelectedText = (selector) => {
            const select = document.querySelector(selector);
            if (!select) return "";
            const option = select.options[select.selectedIndex];
            return (option ? (option.label || option.textContent || option.value || "") : select.value || "").replace(/\\s+/g, " ").trim();
          };

          return {
            row_count: rows.length,
            visible_subjects: Array.from(visibleSubjects),
            selected_course_text: getSelectedText("#idCurso"),
            selected_discipline_text: getSelectedText("#idDisciplina"),
            row_snippets: snippets.slice(0, 12),
          };
        }
        """,
        {"subjects": PORTAL_SUBJECTS},
    )


def validate_filtered_results(
    page: Any,
    logger: Logger,
    portal_subject: str,
) -> None:
    diagnostics = collect_visible_filter_diagnostics(page)
    logger.log(
        f"[{portal_subject}] diagnostico_pos_busca row_count={diagnostics['row_count']} "
        f"disciplinas_visiveis={json.dumps(diagnostics['visible_subjects'], ensure_ascii=False)} "
        f"curso_selecionado={diagnostics['selected_course_text']!s} "
        f"disciplina_selecionada={diagnostics['selected_discipline_text']!s}"
    )
    if int(diagnostics["row_count"]) <= 0:
        logger.log(
            f"[{portal_subject}] WARNING -> busca retornou 0 linhas visiveis; "
            "disciplina sem materiais no momento."
        )
        return
    visible_subjects = [str(item) for item in diagnostics["visible_subjects"]]
    if portal_subject not in visible_subjects:
        raise RuntimeError(
            f"[{portal_subject}] Resultado filtrado incoerente: disciplina alvo nao aparece nas linhas visiveis. "
            f"disciplinas_visiveis={json.dumps(visible_subjects, ensure_ascii=False)}"
        )
    if any(item != portal_subject for item in visible_subjects):
        raise RuntimeError(
            f"[{portal_subject}] Resultado filtrado incoerente: ha disciplinas de outras linhas apos a busca. "
            f"disciplinas_visiveis={json.dumps(visible_subjects, ensure_ascii=False)}"
        )
    if not str(diagnostics["selected_discipline_text"]).strip():
        logger.log(
            f"[{portal_subject}] disciplina_selecionada_pos_submit vazia; "
            "mantendo execucao porque a tabela filtrada esta coerente."
        )
def apply_filters_for_subject(
    page: Any,
    config: PortalRuntimeConfig,
    logger: Logger,
    portal_subject: str,
) -> None:
    before_count = count_visible_result_rows(page)
    logger.log(f"[{portal_subject}] linhas_antes_busca={before_count}")
    discipline_options_before = count_select_options(page, "#idDisciplina")
    logger.log(f"[{portal_subject}] disciplina_options_antes_curso={discipline_options_before}")
    logger.log(
        f"[{portal_subject}] aplicando filtro de curso label={TARGET_COURSE_LABEL} value={TARGET_COURSE_VALUE}"
    )
    course_filter = set_filter_value_by_selector(
        page,
        logger,
        "#idCurso",
        TARGET_COURSE_VALUE,
        by="value",
    )
    discipline_options_after = wait_for_select_options_refresh(
        page,
        config,
        logger,
        "#idDisciplina",
        discipline_options_before,
    )
    logger.log(f"[{portal_subject}] course_value_aplicado={course_filter['applied_value'][:120]}")
    logger.log(f"[{portal_subject}] course_text_resultante={course_filter['applied_text'][:240]}")
    logger.log(f"[{portal_subject}] disciplina_options_depois_curso={discipline_options_after}")
    page.wait_for_timeout(500)

    logger.log(f"[{portal_subject}] aplicando filtro de disciplina={portal_subject}")
    subject_filter = set_filter_value_by_selector(page, logger, "#idDisciplina", portal_subject, by="label")
    logger.log(f"[{portal_subject}] filtro_disciplina_aplicado={subject_filter['applied_text'][:240]}")
    click_search_button(page, logger, portal_subject)
    wait_for_materials_results(page, config, logger, previous_count=before_count)
    after_count = count_visible_result_rows(page)
    logger.log(f"[{portal_subject}] linhas_depois_busca={after_count}")
    validate_filtered_results(page, logger, portal_subject)


def collect_material_rows(page: Any, logger: Logger, portal_subject: str) -> list[PortalRow]:
    raw_rows = page.evaluate(
        """
        () => {
          const normalize = (value) => (value || "")
            .normalize("NFKD")
            .replace(/[\\u0300-\\u036f]/g, "")
            .toLowerCase()
            .replace(/\\s+/g, " ")
            .trim();
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };

          const results = [];
          let position = 0;
          const chosenRows = Array.from(document.querySelectorAll(".material-list-content table tbody tr")).filter((el) => {
            if (!visible(el)) return false;
            const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
            return !!text && text.length <= 2000;
          });

          for (const row of chosenRows) {
            const rowText = (row.innerText || row.textContent || "").replace(/\\s+/g, " ").trim();
            const cells = Array.from(row.querySelectorAll("td"));
            const titleText = ((cells[0]?.innerText || cells[0]?.textContent || "")).replace(/\\s+/g, " ").trim();
            const subjectText = ((cells[2]?.innerText || cells[2]?.textContent || "")).replace(/\\s+/g, " ").trim();
            const rowId = `fo-material-row-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
            row.setAttribute("data-fo-material-row-id", rowId);

            const dateMatch = titleText.match(/\\b\\d{2}\\/\\d{2}\\/\\d{4}(?:\\s+\\d{2}:\\d{2})?\\b/);
            const actions = [];
            let actionPosition = 0;
            for (const node of row.querySelectorAll("a[id^='btnMaterialDownload'], a, button, [role='button'], [onclick], .btn, .dropdown-item")) {
              const clickable = node.closest("a, button, [role='button'], [onclick], .btn, .dropdown-item") || node;
              if (!visible(clickable)) continue;
              const text = (clickable.innerText || clickable.textContent || "").replace(/\\s+/g, " ").trim();
              const title = clickable.getAttribute("title") || clickable.getAttribute("aria-label") || "";
              const href = clickable.getAttribute("href") || "";
              const classes = clickable.className || "";
              const id = clickable.id || "";
              const combined = normalize(`${text} ${title} ${href} ${classes}`);
              if (!combined && !id.startsWith("btnMaterialDownload")) continue;

              let score = 0;
              if (id.startsWith("btnMaterialDownload")) score += 500;
              if (combined.includes("baixar")) score += 300;
              if (combined.includes("download")) score += 250;
              if (combined.includes("arquivo")) score += 40;
              if (combined.includes(".pdf")) score += 60;
              if (href && href !== "#") score += 30;
              if (combined.includes("visualizar")) score += 20;

              const actionId = `fo-material-action-${Date.now()}-${Math.random().toString(36).slice(2, 10)}-${actionPosition++}`;
              clickable.setAttribute("data-fo-action-id", actionId);
              actions.push({
                action_id: actionId,
                action_text: text,
                action_href: href,
                action_tag: (clickable.tagName || "").toLowerCase(),
                action_score: score,
                data_path: clickable.getAttribute("data-path") || "",
                data_token: clickable.getAttribute("data-token") || "",
                data_value: clickable.getAttribute("data-value") || ""
              });
            }

            actions.sort((a, b) => b.action_score - a.action_score);
            const bestAction = actions[0] || {
              action_id: "",
              action_text: "",
              action_href: "",
              action_tag: "",
              action_score: 0
            };

            results.push({
              position: position++,
              row_id: rowId,
              row_text: rowText,
              title_text: titleText,
              subject_text: subjectText,
              raw_date: dateMatch ? dateMatch[0] : "",
              action_id: bestAction.action_id,
              action_text: bestAction.action_text,
              action_href: bestAction.action_href,
              action_tag: bestAction.action_tag,
              action_score: bestAction.action_score,
              data_path: bestAction.data_path || "",
              data_token: bestAction.data_token || "",
              data_value: bestAction.data_value || ""
            });
          }
          return results;
        }
        """,
    )

    rows: list[PortalRow] = []
    for item in raw_rows:
        title_text = str(item.get("title_text") or "")
        row_text = str(item.get("row_text") or "")
        material_type = storage_title_from_portal_title(
            title_text,
            fallback=f"material-{int(item.get('position') or 0) + 1}",
        )
        rows.append(
            PortalRow(
                position=int(item["position"]),
                row_id=str(item["row_id"]),
                row_text=row_text,
                title_text=title_text,
                subject_text=str(item.get("subject_text") or ""),
                material_type=material_type,
                raw_date=str(item.get("raw_date") or ""),
                action_id=str(item.get("action_id") or ""),
                action_text=str(item.get("action_text") or ""),
                action_href=str(item.get("action_href") or ""),
                action_tag=str(item.get("action_tag") or ""),
                action_score=int(item.get("action_score") or 0),
                data_path=str(item.get("data_path") or ""),
                data_token=str(item.get("data_token") or ""),
                data_value=str(item.get("data_value") or ""),
            )
        )

    logger.log(
        f"[{portal_subject}] linhas_coletadas={json.dumps([row.row_text[:240] for row in rows], ensure_ascii=False)}"
    )
    subject_rows = [row for row in rows if normalize_text(row.subject_text) == normalize_text(portal_subject)]
    if len(subject_rows) != len(rows):
        logger.log(
            f"[{portal_subject}] linhas_descartadas_por_disciplina="
            f"{json.dumps([row.row_text[:240] for row in rows if normalize_text(row.subject_text) != normalize_text(portal_subject)], ensure_ascii=False)}"
        )
    ignored_slide_rows = [
        row for row in subject_rows if "slide" in normalize_text(f"{row.title_text} {row.material_type} {row.row_text}")
    ]
    if ignored_slide_rows:
        logger.log(
            f"[{portal_subject}] linhas_ignoradas_por_slide="
            f"{json.dumps([row.row_text[:240] for row in ignored_slide_rows], ensure_ascii=False)}"
        )
    useful_rows = [
        row for row in subject_rows if "slide" not in normalize_text(f"{row.title_text} {row.material_type} {row.row_text}")
    ]
    return useful_rows


def select_latest_rows(
    rows: list[PortalRow],
    logger: Logger,
    portal_subject: str,
) -> dict[str, PortalRow]:
    selected: dict[str, PortalRow] = {}
    for material_type in CANONICAL_TYPES:
        candidates = [row for row in rows if row.material_type == material_type]
        if not candidates:
            continue
        candidates.sort(
            key=lambda row: (
                parse_portal_date(row.raw_date) or datetime.min,
                row.action_score,
                -row.position,
            ),
            reverse=True,
        )
        selected[material_type] = candidates[0]
        logger.log(
            f"[{portal_subject}] selecionado tipo={material_type} "
            f"data={candidates[0].raw_date or '-'} row={candidates[0].row_text[:240]}"
        )
    return selected


def rebind_row_action(
    page: Any,
    logger: Logger,
    portal_subject: str,
    material_type: str,
    raw_date: str,
) -> PortalRow | None:
    rows = collect_material_rows(page, logger, portal_subject)
    candidates = [row for row in rows if row.material_type == material_type]
    if raw_date:
        exact = [row for row in candidates if row.raw_date == raw_date]
        if exact:
            return exact[0]
    return candidates[0] if candidates else None


def download_via_native_download(
    page: Any,
    row: PortalRow,
    config: PortalRuntimeConfig,
    destination: Path,
) -> str | None:
    if not row.action_id:
        return None
    action = page.locator(f"[data-fo-action-id='{row.action_id}']").first
    action.scroll_into_view_if_needed(timeout=5_000)
    with page.expect_download(timeout=config.download_timeout_ms) as download_info:
        action.click(timeout=5_000)
    download = download_info.value
    download.save_as(str(destination))
    source_url = row.action_href or getattr(download, "url", "") or page.url
    return str(source_url)


def download_via_fallback_fetch(
    page: Any,
    row: PortalRow,
    logger: Logger,
    destination: Path,
) -> str | None:
    if row.action_href and row.action_href != "#":
        resolved_href = urljoin(page.url, row.action_href)
        try:
            raw_bytes, source_url = capture_pdf_bytes_from_url(page, resolved_href)
            if raw_bytes.startswith(b"%PDF-"):
                destination.write_bytes(raw_bytes)
                return source_url
        except Exception as exc:
            logger.log(f"[fallback_fetch] href falhou detalhe={summarize_exception(exc)}")

    existing_pages = list(page.context.pages)
    if row.data_path or row.data_token or row.data_value:
        logger.log(
            "[fallback_fetch] usando atributos reais da linha "
            f"data_path={row.data_path} data_token={row.data_token[:32]} data_value={row.data_value}"
        )
    if row.action_id:
        action = page.locator(f"[data-fo-action-id='{row.action_id}']").first
        try:
            action.scroll_into_view_if_needed(timeout=5_000)
            action.click(timeout=5_000)
        except Exception as exc:
            logger.log(f"[fallback_fetch] click pelo action_id falhou detalhe={summarize_exception(exc)}")

    if row.data_path or row.data_token or row.data_value:
        try:
            page.evaluate(
                """
                ({ dataPath, dataToken, dataValue }) => {
                  const candidates = Array.from(document.querySelectorAll("a[id^='btnMaterialDownload']"));
                  const match = candidates.find((el) =>
                    (dataPath ? (el.getAttribute("data-path") || "") === dataPath : true) &&
                    (dataToken ? (el.getAttribute("data-token") || "") === dataToken : true) &&
                    (dataValue ? (el.getAttribute("data-value") || "") === dataValue : true)
                  );
                  if (!match) {
                    throw new Error("botao de download nao encontrado pelos atributos reais");
                  }
                  match.click();
                }
                """,
                {
                    "dataPath": row.data_path,
                    "dataToken": row.data_token,
                    "dataValue": row.data_value,
                },
            )
        except Exception as exc:
            logger.log(f"[fallback_fetch] click por atributos reais falhou detalhe={summarize_exception(exc)}")
    page.wait_for_timeout(1_500)

    candidate_pages = [item for item in page.context.pages if item not in existing_pages]
    if not candidate_pages:
        candidate_pages = [page]

    try:
        for candidate_page in candidate_pages:
            urls = extract_pdf_urls(candidate_page)
            if candidate_page.url not in urls:
                urls.insert(0, candidate_page.url)
            for pdf_url in urls:
                try:
                    raw_bytes, source_url = capture_pdf_bytes_from_url(candidate_page, pdf_url)
                    if raw_bytes.startswith(b"%PDF-"):
                        destination.write_bytes(raw_bytes)
                        return source_url
                except Exception:
                    continue
    finally:
        for candidate_page in candidate_pages:
            if candidate_page is not page:
                try:
                    candidate_page.close()
                except Exception:
                    pass
    return None


def download_material_row(
    page: Any,
    config: PortalRuntimeConfig,
    logger: Logger,
    portal_subject: str,
    row: PortalRow,
    destination: Path,
) -> str:
    try:
        logger.log(
            f"[{portal_subject}] tentando download nativo tipo={row.material_type} "
            f"data={row.raw_date or '-'} action={row.action_text or row.action_href or row.action_tag}"
        )
        source_url = download_via_native_download(page, row, config, destination)
        if source_url and destination.exists():
            return source_url
    except PlaywrightTimeoutError as exc:
        logger.log(
            f"[{portal_subject}] expect_download nao disparou; tentando fallback. "
            f"detalhe={summarize_exception(exc)}"
        )
    except Exception as exc:
        logger.log(
            f"[{portal_subject}] download nativo falhou; tentando fallback. "
            f"detalhe={summarize_exception(exc)}"
        )

    source_url = download_via_fallback_fetch(page, row, logger, destination)
    if source_url and destination.exists():
        return source_url
    raise RuntimeError("Nenhuma estrategia de download conseguiu obter o PDF.")


def append_error_entry(entries: list[ManifestEntry], target: PortalTarget, started_at: str, error: str) -> None:
    entries.append(
        ManifestEntry(
            target_key=target.target_key,
            group=target.group,
            portal_subject=target.portal_subject,
            output_subject=target.output_subject,
            material_type=target.material_type,
            status="error",
            file_name="",
            relative_path="",
            sha256="",
            byte_size=0,
            source_url="",
            source_label="",
            started_at=started_at,
            finished_at=now_utc_iso(),
            error=error,
        )
    )


def process_subject(
    context: Any,
    config: PortalRuntimeConfig,
    logger: Logger,
    subject_targets: list[PortalTarget],
    target_dir: Path,
    raw_root: Path,
    hashes_by_sha: dict[str, str],
    entries: list[ManifestEntry],
) -> None:
    base_target = subject_targets[0]
    portal_subject = base_target.portal_subject
    area_dir = target_dir / subject_area_for(portal_subject)
    page = open_authenticated_materials_page(context, config, logger)
    try:
        apply_filters_for_subject(page, config, logger, portal_subject)
        rows = collect_material_rows(page, logger, portal_subject)
        if base_target.material_type:
            filter_norm = normalize_text(base_target.material_type)
            filtered_rows = [
                row
                for row in rows
                if filter_norm in normalize_text(f"{row.title_text} {row.material_type} {row.row_text}")
            ]
            logger.log(
                f"[{portal_subject}] filtro_legado_only_type={base_target.material_type} "
                f"linhas_antes={len(rows)} linhas_depois={len(filtered_rows)}"
            )
            rows = filtered_rows
        if not rows:
            logger.log(f"[{portal_subject}] WARNING -> nenhuma linha visivel para a disciplina filtrada.")
            return

        area_dir.mkdir(parents=True, exist_ok=True)
        for selected_row in rows:
            row_target = PortalTarget(
                group=base_target.group,
                portal_subject=base_target.portal_subject,
                output_subject=base_target.output_subject,
                material_type=selected_row.material_type,
            )
            started_at = now_utc_iso()
            logger.log(f"[{row_target.target_key}] iniciando.")
            rebound_row = rebind_row_action(
                page,
                logger,
                portal_subject,
                selected_row.material_type,
                selected_row.raw_date,
            )
            row = rebound_row or selected_row
            if not row.action_id and not row.action_href:
                append_error_entry(
                    entries,
                    row_target,
                    started_at,
                    "Linha selecionada nao possui acao clara de download/visualizacao.",
                )
                logger.log(f"[{row_target.target_key}] ERRO -> linha sem acao de download.")
                continue

            destination = unique_destination_for_filename(
                area_dir,
                filename_for_row(portal_subject, row.material_type),
            )
            temp_destination = destination.with_suffix(".tmp")
            temp_destination.unlink(missing_ok=True)
            try:
                source_label = (
                    f"{TARGET_COURSE_LABEL} | {row_target.portal_subject} | {row.material_type} | "
                    f"{row.raw_date or '-'} | {row.row_text[:240]}"
                )
                logger.log(
                    f"[{row_target.target_key}] linha escolhida data={row.raw_date or '-'} "
                    f"acao={row.action_text or row.action_href or row.action_tag}"
                )
                source_url = download_material_row(page, config, logger, portal_subject, row, temp_destination)
                if not temp_destination.exists() or not is_pdf_file(temp_destination):
                    raise RuntimeError("Arquivo baixado nao possui cabecalho PDF valido.")

                sha = sha256_file(temp_destination)
                duplicate_of = material_duplicate_conflicts(hashes_by_sha, sha, row_target)
                if duplicate_of:
                    temp_destination.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Hash duplicado com outro alvo ja salvo "
                        f"({duplicate_of}); pulando para evitar nomear PDF errado."
                    )

                temp_destination.rename(destination)
                entries.append(
                    ManifestEntry(
                        target_key=row_target.target_key,
                        group=row_target.group,
                        portal_subject=row_target.portal_subject,
                        output_subject=row_target.output_subject,
                        material_type=row.material_type,
                        status="ok",
                        file_name=destination.name,
                        relative_path=str(destination.relative_to(raw_root)),
                        sha256=sha,
                        byte_size=destination.stat().st_size,
                        source_url=source_url,
                        source_label=source_label,
                        started_at=started_at,
                        finished_at=now_utc_iso(),
                        error="",
                    )
                )
                logger.log(
                    f"[{row_target.target_key}] OK -> {destination.name} "
                    f"(sha256={sha[:12]}..., bytes={destination.stat().st_size})"
                )
            except Exception as exc:
                temp_destination.unlink(missing_ok=True)
                logger.log(f"[{row_target.target_key}] ERRO -> {exc}")
                append_error_entry(entries, row_target, started_at, str(exc))
    finally:
        try:
            page.close()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    try:
        config = build_runtime_config(args)
    except Exception as exc:
        print(f"Erro de configuracao: {exc}", file=sys.stderr)
        return 2

    if sync_playwright is None:
        print(
            "Dependencia ausente: playwright.\n"
            "Instale na VPS com:\n"
            "  python3 -m pip install playwright\n"
            "  python3 -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    config.state_root.mkdir(parents=True, exist_ok=True)
    config.persistent_logs_dir.mkdir(parents=True, exist_ok=True)

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = Logger([config.persistent_log_file])
    logger.log("Inicio da sincronizacao de materiais do Federal Online via /portal/materiais.")
    logger.log(f"Arquivo de configuracao: {config.env_file}")
    logger.log(f"Diretorio de estado: {config.state_root}")
    logger.log(f"Diretorio final de materiais: {config.final_dir}")
    logger.log(f"Portal de materiais: {config.materials_url}")
    logger.log(f"Log persistente: {config.persistent_log_file}")

    temp_build_dir = config.state_root / f".build_materiais_fo_{run_stamp}_{os.getpid()}"
    temp_build_dir.mkdir(parents=True, exist_ok=True)
    logger.log(f"Diretorio temporario da execucao: {temp_build_dir}")

    entries: list[ManifestEntry] = []
    hashes_by_sha: dict[str, str] = {}

    browser = None
    playwright = None
    context = None
    promoted = False
    try:
        cleanup_stale_build_dirs(config.state_root, logger, keep={temp_build_dir})
        targets = build_targets(material_filter=args.only_type)
        if args.only_subject:
            targets = [target for target in targets if target.portal_subject == args.only_subject]
        if not targets:
            raise RuntimeError("Nenhum alvo selecionado para execucao com os filtros atuais.")

        subject_groups: dict[str, list[PortalTarget]] = {}
        for target in targets:
            subject_groups.setdefault(target.portal_subject, []).append(target)

        logger.log(
            "Alvos selecionados: "
            f"{len(targets)} | disciplinas={len(subject_groups)} "
            f"| filtro_subject={args.only_subject or '-'} | filtro_type_legado={args.only_type or '-'}"
        )

        playwright = logged_step(
            logger,
            "main.sync_playwright.start",
            "playwright",
            None,
            lambda: sync_playwright().start(),
        )
        browser = logged_step(
            logger,
            "main.chromium.launch",
            f"headless={config.headless}",
            None,
            lambda: playwright.chromium.launch(headless=config.headless),
        )

        context_kwargs: dict[str, Any] = {"accept_downloads": True}
        protect_storage_state(config.storage_state_file)
        if config.storage_state_file.exists():
            context_kwargs["storage_state"] = str(config.storage_state_file)
            logger.log(f"Carregando storage_state existente: {config.storage_state_file}")
        else:
            logger.log("Nenhum storage_state anterior encontrado; seguindo sem sessao persistida.")

        context = logged_step(
            logger,
            "main.browser.new_context",
            "contexto-playwright",
            None,
            lambda: browser.new_context(**context_kwargs),
        )

        seed_page = open_authenticated_materials_page(context, config, logger)
        save_storage_state(context, config.storage_state_file)
        logger.log(f"storage_state persistido apos seed em {config.storage_state_file}")
        seed_page.close()

        for portal_subject in PORTAL_SUBJECTS:
            if portal_subject not in subject_groups:
                continue
            process_subject(
                context=context,
                config=config,
                logger=logger,
                subject_targets=subject_groups[portal_subject],
                target_dir=temp_build_dir,
                raw_root=temp_build_dir,
                hashes_by_sha=hashes_by_sha,
                entries=entries,
            )

        write_manifest(temp_build_dir, entries)
        logger.log("Manifest gravado com sucesso.")
        if context is not None:
            context.close()
            context = None
        logger.log(
            "Resumo: "
            f"{sum(1 for entry in entries if entry.status == 'ok')} OK, "
            f"{sum(1 for entry in entries if entry.status != 'ok')} erro(s)."
        )
        logger.log("etapa=promover_materiais_fo.inicio")
        promote_output_dir(config.state_root, temp_build_dir, config.final_dir)
        promoted = True
        logger.log("etapa=promover_materiais_fo.ok")
        cleanup_stale_build_dirs(config.state_root, logger)
        if args.sync_onedrive:
            sync_to_onedrive(config.final_dir, logger)
        return 0
    except Exception as exc:
        logger.log(f"Falha fatal da sincronizacao: {exc}")
        logger.log(traceback.format_exc())
        if temp_build_dir.exists():
            write_manifest(temp_build_dir, entries)
        return 1
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        if not promoted and temp_build_dir.exists():
            shutil.rmtree(temp_build_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
