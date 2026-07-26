#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fo_contracts import MATERIAL_MANIFEST_SCHEMA_VERSION, validate_material_manifest_payload
from fo_session_security import protect_storage_state, save_storage_state

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - handled at runtime on the VPS.
    PlaywrightTimeoutError = RuntimeError  # type: ignore[assignment]
    sync_playwright = None


DEFAULT_ENV_FILE = Path("/home/ubuntu/.config/anki-gpt-sync/federal_online.env")
DEFAULT_STATE_ROOT = Path("/home/ubuntu/anki-gpt-sync/state/federal_online")
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_DOWNLOAD_TIMEOUT_MS = 45_000
LOGIN_PATH_FRAGMENT = "/portal/login"

DISCIPLINAS = [
    "Biologia I",
    "Biologia II",
    "Biologia III",
    "Física I",
    "Física II",
    "Física III",
    "Geografia I",
    "Geografia II",
    "Geografia III",
    "História I",
    "História II",
    "Matemática I",
    "Matemática II",
    "Matemática III",
    "Português I",
    "Português II",
    "Química I",
    "Química II",
    "Química III",
]

OBRAS = [
    ("Filosofia UFPR", "Filosofia Obras"),
    ("Sociologia UFPR", "Sociologia Obras"),
    ("Literatura UFPR", "Literatura Obras"),
]

TIPOS = [
    "Lista de exercícios",
    "Resumo",
    "Gabarito comentado",
]

LOGIN_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[name*='login' i]",
    "input[id*='login' i]",
    "input[name*='usuario' i]",
    "input[id*='usuario' i]",
]

LOGIN_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name*='senha' i]",
    "input[id*='senha' i]",
    "input[name*='password' i]",
    "input[id*='password' i]",
]

LOGIN_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Entrar')",
    "button:has-text('Login')",
    "button:has-text('Acessar')",
    "button:has-text('Continuar')",
]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def slugify(text: str) -> str:
    value = normalize_text(text)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    return value or "item"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not match:
            raise ValueError(f"Linha invalida no env: {raw_line}")
        key, raw_value = match.groups()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class MaterialTarget:
    group: str
    portal_subject: str
    output_subject: str
    material_type: str

    @property
    def slug(self) -> str:
        return slugify(f"{self.output_subject} {self.material_type}")

    @property
    def filename(self) -> str:
        return f"{self.slug}.pdf"

    @property
    def target_key(self) -> str:
        return f"{self.output_subject} | {self.material_type}"

    @property
    def normalized_subject(self) -> str:
        return normalize_text(self.portal_subject)

    @property
    def normalized_type(self) -> str:
        return normalize_text(self.material_type)

    @property
    def portal_path(self) -> tuple[str, ...]:
        if self.group == "obra":
            return ("Obras", self.portal_subject)

        match = re.match(r"^(.*)\s+([IVX]+)$", self.portal_subject)
        if match:
            return (match.group(1), self.portal_subject)
        return (self.portal_subject,)


@dataclass
class Candidate:
    node_id: str
    text: str
    href: str
    tag: str

    @property
    def searchable_text(self) -> str:
        return normalize_text(f"{self.text} {self.href}")


@dataclass
class DownloadResult:
    source_url: str
    source_label: str
    sha256: str
    byte_size: int
    file_name: str
    relative_path: str


@dataclass
class TreeMatch:
    card_id: str
    header_id: str
    text: str
    data_target: str
    body_id: str


@dataclass
class MaterialMatch:
    item_id: str
    text: str
    onclick: str


@dataclass(frozen=True)
class ViewerState:
    lesson_name: str
    src: str


@dataclass
class CardBodyState:
    body_id: str
    exists: bool
    is_open: bool
    body_class: str
    body_html: str
    body_text: str
    clickable_items: list[dict[str, str]]


@dataclass
class ManifestEntry:
    target_key: str
    group: str
    portal_subject: str
    output_subject: str
    material_type: str
    status: str
    file_name: str
    relative_path: str
    sha256: str
    byte_size: int
    source_url: str
    source_label: str
    started_at: str
    finished_at: str
    error: str


@dataclass(frozen=True)
class RuntimeConfig:
    env_file: Path
    state_root: Path
    storage_state_file: Path
    raw_dir: Path
    files_dir: Path
    logs_dir: Path
    log_file: Path
    persistent_logs_dir: Path
    persistent_log_file: Path
    course_url: str
    login_url: str | None
    email: str
    password: str
    headless: bool
    nav_timeout_ms: int
    download_timeout_ms: int


class Logger:
    def __init__(self, log_files: list[Path]) -> None:
        self.log_files: list[Path] = []
        for log_file in log_files:
            self.add_log_file(log_file)

    def add_log_file(self, log_file: Path) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file not in self.log_files:
            self.log_files.append(log_file)

    def remove_log_file(self, log_file: Path) -> None:
        self.log_files = [item for item in self.log_files if item != log_file]

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line)
        for log_file in self.log_files:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def summarize_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def logged_step(
    logger: Logger,
    func_name: str,
    target: str,
    timeout_ms: int | None,
    action: Any,
) -> Any:
    timeout_info = f" timeout_ms={timeout_ms}" if timeout_ms is not None else ""
    logger.log(f"[{func_name}] inicio target={target}{timeout_info}")
    try:
        result = action()
    except Exception as exc:
        logger.log(
            f"[{func_name}] erro target={target}{timeout_info} exc={summarize_exception(exc)}"
        )
        raise
    logger.log(f"[{func_name}] ok target={target}")
    return result


def build_targets() -> list[MaterialTarget]:
    targets: list[MaterialTarget] = []
    for disciplina in DISCIPLINAS:
        for material_type in TIPOS:
            targets.append(
                MaterialTarget(
                    group="disciplina",
                    portal_subject=disciplina,
                    output_subject=disciplina,
                    material_type=material_type,
                )
            )
    for portal_subject, output_subject in OBRAS:
        for material_type in TIPOS:
            targets.append(
                MaterialTarget(
                    group="obra",
                    portal_subject=portal_subject,
                    output_subject=output_subject,
                    material_type=material_type,
                )
            )
    return targets


def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    env_values = parse_env_file(args.env_file)

    missing = [
        key
        for key in (
            "FEDERAL_ONLINE_EMAIL",
            "FEDERAL_ONLINE_PASSWORD",
            "FEDERAL_ONLINE_COURSE_URL",
        )
        if not env_values.get(key)
    ]
    if missing:
        raise ValueError(
            "Variaveis obrigatorias ausentes em "
            f"{args.env_file}: {', '.join(missing)}"
        )

    state_root = args.state_root
    raw_dir = state_root / "raw"
    logs_dir = raw_dir / "logs"
    persistent_logs_dir = state_root / "logs"
    return RuntimeConfig(
        env_file=args.env_file,
        state_root=state_root,
        storage_state_file=state_root / "storage_state.json",
        raw_dir=raw_dir,
        files_dir=raw_dir / "files",
        logs_dir=logs_dir,
        log_file=logs_dir / "fo_sync_materials.log",
        persistent_logs_dir=persistent_logs_dir,
        persistent_log_file=persistent_logs_dir / "fo_sync_materials.latest.log",
        course_url=env_values["FEDERAL_ONLINE_COURSE_URL"],
        login_url=env_values.get("FEDERAL_ONLINE_LOGIN_URL"),
        email=env_values["FEDERAL_ONLINE_EMAIL"],
        password=env_values["FEDERAL_ONLINE_PASSWORD"],
        headless=not args.headed,
        nav_timeout_ms=args.nav_timeout_ms,
        download_timeout_ms=args.download_timeout_ms,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sincroniza materiais PDF do Federal Online para a VPS."
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
        help="Executa apenas um subject especifico, ex: 'Biologia I'.",
    )
    parser.add_argument(
        "--only-type",
        default="",
        help="Executa apenas um tipo especifico, ex: 'Resumo'.",
    )
    return parser.parse_args()


def page_requires_login(page: Any) -> bool:
    current_url = normalize_text(page.url)
    if LOGIN_PATH_FRAGMENT in current_url:
        return True
    return page.locator("input[type='password']").count() > 0


def first_existing_locator(page: Any, selectors: list[str]) -> Any:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    raise RuntimeError(f"Nenhum seletor encontrado: {selectors}")


def login_if_needed(page: Any, config: RuntimeConfig, logger: Logger) -> None:
    if not page_requires_login(page):
        return

    logger.log("Sessao expirada ou ausente; realizando login automatico.")

    email_input = first_existing_locator(page, LOGIN_EMAIL_SELECTORS)
    password_input = first_existing_locator(page, LOGIN_PASSWORD_SELECTORS)
    submit_button = first_existing_locator(page, LOGIN_SUBMIT_SELECTORS)

    email_input.fill(config.email)
    password_input.fill(config.password)
    submit_button.click()

    try:
        logged_step(
            logger,
            "login_if_needed.wait_for_url",
            "redirect-pos-login",
            config.nav_timeout_ms,
            lambda: page.wait_for_url(
                lambda url: LOGIN_PATH_FRAGMENT not in url,
                timeout=config.nav_timeout_ms,
            ),
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Login nao concluiu dentro do timeout.") from exc

    logged_step(
        logger,
        "login_if_needed.wait_for_load_state",
        "networkidle-pos-login",
        config.nav_timeout_ms,
        lambda: page.wait_for_load_state("networkidle", timeout=config.nav_timeout_ms),
    )
    save_storage_state(page.context, config.storage_state_file)
    logger.log(f"storage_state atualizado em {config.storage_state_file}")


def open_authenticated_course_page(context: Any, config: RuntimeConfig, logger: Logger) -> Any:
    page = context.new_page()
    page.set_default_timeout(config.nav_timeout_ms)
    logged_step(
        logger,
        "open_authenticated_course_page.goto",
        config.course_url,
        config.nav_timeout_ms,
        lambda: page.goto(
            config.course_url,
            wait_until="domcontentloaded",
            timeout=config.nav_timeout_ms,
        ),
    )

    if page_requires_login(page):
        if config.login_url:
            logger.log("Acesso direto caiu no login; abrindo login_url configurada.")
            logged_step(
                logger,
                "open_authenticated_course_page.goto_login",
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
            "open_authenticated_course_page.goto_course_after_login",
            config.course_url,
            config.nav_timeout_ms,
            lambda: page.goto(
                config.course_url,
                wait_until="domcontentloaded",
                timeout=config.nav_timeout_ms,
            ),
        )
    else:
        logger.log("Acesso direto ao curso funcionou com a sessao atual.")

    logged_step(
        logger,
        "open_authenticated_course_page.wait_for_load_state",
        "load-no-curso",
        config.nav_timeout_ms,
        lambda: page.wait_for_load_state("load", timeout=config.nav_timeout_ms),
    )
    try:
        logged_step(
            logger,
            "open_authenticated_course_page.wait_for_course_selector",
            ".header-wrapper|.card",
            config.nav_timeout_ms,
            lambda: page.wait_for_function(
                """
                () => {
                  return (
                    document.querySelectorAll(".header-wrapper").length > 0 ||
                    document.querySelectorAll(".card").length > 0
                  );
                }
                """,
                timeout=config.nav_timeout_ms,
            ),
        )
    except Exception as exc:
        logger.log(
            "Seletor concreto do curso nao estabilizou; seguindo com delay defensivo. "
            f"detalhe={summarize_exception(exc)}"
        )
        logged_step(
            logger,
            "open_authenticated_course_page.wait_for_timeout",
            "delay-defensivo-no-curso",
            1_000,
            lambda: page.wait_for_timeout(1_000),
        )
    neutralize_intro_overlay(page, logger)
    return page


def neutralize_intro_overlay(page: Any, logger: Logger) -> None:
    try:
        for label in ("Fechar", "Pular", "Skip", "Close", "×"):
            locator = page.get_by_text(label, exact=True).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=1_500)
                    page.wait_for_timeout(300)
                    logger.log(f"Overlay Intro.js fechado por botao: {label}")
                    break
            except Exception:
                continue
    except Exception:
        pass

    removed = page.evaluate(
        """
        () => {
          const selectors = [
            ".introjs-overlay",
            ".introjs-helperLayer",
            ".introjs-tooltipReferenceLayer",
            ".introjs-tooltip",
            ".introjs-showElement"
          ];
          let removed = 0;
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              node.remove();
              removed += 1;
            }
          }
          return removed;
        }
        """
    )
    if removed:
        logger.log(f"Overlay Intro.js neutralizado via DOM; elementos removidos: {removed}")


def resolve_scope_selector(data_target: str, body_id: str) -> tuple[str | None, str | None]:
    if data_target:
        return data_target, "data_target"
    if body_id:
        return f"#{body_id}", "body_id"
    return None, None


def find_tree_header(page: Any, title: str, parent_scope_selector: str | None = None) -> TreeMatch | None:
    raw = page.evaluate(
        """
        ({ title, parentScopeSelector }) => {
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
          const root = parentScopeSelector
            ? document.querySelector(parentScopeSelector)
            : document;
          if (!root) return null;

          const selector = parentScopeSelector ? ".card .header-wrapper" : ".header-wrapper";
          const titleNorm = normalize(title);
          let best = null;

          for (const header of root.querySelectorAll(selector)) {
            if (!visible(header)) continue;
            const text = normalize(header.innerText || header.textContent || "");
            if (!text || !text.includes(titleNorm)) continue;

            const card = header.closest(".card") || header.parentElement;
            if (!card) continue;

            const score =
              (text === titleNorm ? 1000 : 500) -
              Math.abs(text.length - titleNorm.length);

            if (!best || score > best.score) {
              best = {
                header,
                card,
                score,
                text: (header.innerText || header.textContent || "").trim(),
              };
            }
          }

          if (!best) return null;

          const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
          const cardId = `fo-card-${stamp}`;
          const headerId = `fo-header-${stamp}`;
          best.card.setAttribute("data-fo-card-id", cardId);
          best.header.setAttribute("data-fo-header-id", headerId);
          const body =
            best.card.querySelector(":scope > .card-body, :scope > .collapse, :scope > .accordion-collapse") ||
            Array.from(best.card.children).find((el) =>
              (el.classList && (el.classList.contains("card-body") || el.classList.contains("collapse") || el.classList.contains("accordion-collapse")))
            ) ||
            best.card.querySelector(".card-body, .collapse, .accordion-collapse");
          return {
            card_id: cardId,
            header_id: headerId,
            text: best.text,
            data_target: best.header.getAttribute("data-target") || best.header.getAttribute("href") || "",
            body_id: body ? (body.id || "") : ""
          };
        }
        """,
        {"title": title, "parentScopeSelector": parent_scope_selector},
    )
    return TreeMatch(**raw) if raw else None


def get_card_body_state(page: Any, card_id: str) -> CardBodyState:
    raw = page.evaluate(
        """
        ({ cardId }) => {
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
          const root = document.querySelector(`[data-fo-card-id="${cardId}"]`);
          if (!root) {
            return {
              body_id: "",
              exists: false,
              is_open: false,
              body_class: "",
              body_html: "",
              body_text: "",
              clickable_items: []
            };
          }

          const body =
            root.querySelector(":scope > .card-body, :scope > .collapse, :scope > .accordion-collapse") ||
            Array.from(root.children).find((el) =>
              (el.classList && (el.classList.contains("card-body") || el.classList.contains("collapse") || el.classList.contains("accordion-collapse")))
            ) ||
            root.querySelector(".card-body, .collapse, .accordion-collapse");

          if (!body) {
            return {
              body_id: "",
              exists: false,
              is_open: false,
              body_class: "",
              body_html: "",
              body_text: "",
              clickable_items: []
            };
          }

          const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
          const bodyId = `fo-body-${stamp}`;
          body.setAttribute("data-fo-body-id", bodyId);

          const clickables = [];
          for (const node of body.querySelectorAll("a, button, [role='button'], [onclick], li, .item, .material, .material-item, .panel-item")) {
            if (!visible(node)) continue;
            const text = (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim();
            if (!text || text.length > 300) continue;
            clickables.push({
              tag: (node.tagName || "").toLowerCase(),
              text,
              href: node.getAttribute("href") || ""
            });
            if (clickables.length >= 30) break;
          }

          return {
            body_id: bodyId,
            exists: true,
            is_open: (body.className || "").includes("show"),
            body_class: body.className || "",
            body_html: body.innerHTML || "",
            body_text: body.innerText || body.textContent || "",
            clickable_items: clickables
          };
        }
        """,
        {"cardId": card_id},
    )
    return CardBodyState(**raw)


def body_contains_text(state: CardBodyState, text: str) -> bool:
    return normalize_text(text) in normalize_text(state.body_text)


def click_tree_header(page: Any, match: TreeMatch, logger: Logger) -> None:
    header = page.locator(f"[data-fo-header-id='{match.header_id}']").first
    logged_step(
        logger,
        "click_tree_header.scroll_into_view",
        match.text,
        5_000,
        lambda: header.scroll_into_view_if_needed(timeout=5_000),
    )
    try:
        logged_step(
            logger,
            "click_tree_header.click",
            match.text,
            5_000,
            lambda: header.click(timeout=5_000),
        )
    except Exception as exc:
        logger.log(f"Click normal do card falhou; tentando fallback JS. detalhe={exc}")
        logged_step(
            logger,
            "click_tree_header.js_fallback",
            match.text,
            None,
            lambda: page.evaluate(
                """
                ({ headerId }) => {
                  const el = document.querySelector(`[data-fo-header-id="${headerId}"]`);
                  if (!el) {
                    throw new Error("header-wrapper nao encontrado para fallback JS");
                  }
                  el.click();
                }
                """,
                {"headerId": match.header_id},
            ),
        )


def ensure_card_open(page: Any, logger: Logger, target: MaterialTarget, match: TreeMatch, next_expectation: str) -> CardBodyState:
    state = get_card_body_state(page, match.card_id)
    if state.exists and state.is_open:
        logger.log(f"[{target.target_key}] card ja aberto: {match.text}")
        return state

    logger.log(f"[{target.target_key}] abrindo card: {match.text}")
    click_tree_header(page, match, logger)
    page.wait_for_timeout(800)
    neutralize_intro_overlay(page, logger)

    state = get_card_body_state(page, match.card_id)
    if state.exists and state.is_open:
        return state

    raise RuntimeError(f"Card nao abriu corretamente: {match.text}")


def ensure_tree_path_open(page: Any, logger: Logger, target: MaterialTarget) -> TreeMatch:
    current_scope_selector: str | None = None
    current_match: TreeMatch | None = None

    for index, title in enumerate(target.portal_path):
        neutralize_intro_overlay(page, logger)
        match = find_tree_header(page, title, current_scope_selector)
        if not match and index > 0:
            current_scope_selector = None
            match = find_tree_header(page, title, None)
        if not match:
            raise RuntimeError("Disciplina/obra nao localizada na pagina do curso.")

        next_expectation = (
            target.portal_path[index + 1]
            if index + 1 < len(target.portal_path)
            else target.material_type
        )

        state = get_card_body_state(page, match.card_id)
        if not state.exists or not state.is_open:
            state = ensure_card_open(page, logger, target, match, next_expectation)

        current_scope_selector, scope_origin = resolve_scope_selector(match.data_target, match.body_id)
        logger.log(
            f"[{target.target_key}] scope_atual_novo={current_scope_selector or '(vazio)'} "
            f"origem_scope={scope_origin or 'none'}"
        )
        current_match = match

    if current_match is None:
        raise RuntimeError("Falha interna ao resolver caminho da arvore.")
    return current_match


def log_card_body_probe(page: Any, logger: Logger, target: MaterialTarget, card_id: str) -> CardBodyState:
    state = get_card_body_state(page, card_id)
    body_text = re.sub(r"\s+", " ", state.body_text).strip()
    body_html = re.sub(r"\s+", " ", state.body_html).strip()
    clickables = json.dumps(state.clickable_items, ensure_ascii=False)
    logger.log(f"[{target.target_key}] body_class={state.body_class}")
    logger.log(f"[{target.target_key}] body_text={body_text[:1200]}")
    logger.log(f"[{target.target_key}] body_html={body_html[:1200]}")
    logger.log(f"[{target.target_key}] body_clickables={clickables[:2000]}")
    return state


def find_material_item(page: Any, card_id: str, material_type: str) -> MaterialMatch | None:
    raw = page.evaluate(
        """
        ({ cardId, materialType }) => {
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
          const rootCard = document.querySelector(`[data-fo-card-id="${cardId}"]`);
          if (!rootCard) return null;
          const root = rootCard.querySelector("[data-fo-body-id]") || rootCard.querySelector(":scope > .card-body, :scope > .collapse, :scope > .accordion-collapse") || rootCard;

          const targetNorm = normalize(materialType);
          const allowed = new Set([
            normalize("Resumo"),
            normalize("Lista de exercícios"),
            normalize("Gabarito comentado")
          ]);
          if (!allowed.has(targetNorm)) return null;

          const parseOpenMedia4 = (onclick) => {
            const match = onclick.match(
              /openMedia4\\s*\\(\\s*event\\s*,\\s*(['"])([^'"]+)\\1\\s*,\\s*(['"])([^'"]+)\\3\\s*,\\s*(['"])([^'"]+)\\5\\s*\\)/i
            );
            if (!match) return null;
            return {
              itemKey: match[2],
              materialName: match[4],
              mediaType: match[6]
            };
          };

          for (const item of root.querySelectorAll("li[onclick]")) {
            if (!visible(item)) continue;
            const onclick = item.getAttribute("onclick") || "";
            const parsed = parseOpenMedia4(onclick);
            if (!parsed) continue;
            if (normalize(parsed.mediaType) !== "d") continue;

            const materialNameNorm = normalize(parsed.materialName);
            if (!allowed.has(materialNameNorm)) continue;
            if (materialNameNorm !== targetNorm) continue;

            const itemId = `fo-material-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
            item.setAttribute("data-fo-material-id", itemId);
            return {
              item_id: itemId,
              text: (item.innerText || item.textContent || parsed.materialName || "").trim(),
              onclick
            };
          }
          return null;
        }
        """,
        {"cardId": card_id, "materialType": material_type},
    )
    return MaterialMatch(**raw) if raw else None


def click_material_item(page: Any, match: MaterialMatch, logger: Logger) -> None:
    item = page.locator(f"[data-fo-material-id='{match.item_id}']").first
    logged_step(
        logger,
        "click_material_item.scroll_into_view",
        match.text,
        5_000,
        lambda: item.scroll_into_view_if_needed(timeout=5_000),
    )
    try:
        logged_step(
            logger,
            "click_material_item.click",
            match.text,
            5_000,
            lambda: item.click(timeout=5_000),
        )
    except Exception as exc:
        logger.log(f"Click normal do material falhou; tentando fallback JS. detalhe={exc}")
        try:
            logged_step(
                logger,
                "click_material_item.js_fallback",
                match.text,
                None,
                lambda: page.evaluate(
                    """
                    ({ itemId }) => {
                      const el = document.querySelector(`[data-fo-material-id="${itemId}"]`);
                      if (!el) {
                        throw new Error("material nao encontrado para fallback JS");
                      }
                      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                    }
                    """,
                    {"itemId": match.item_id},
                ),
            )
        except Exception:
            logged_step(
                logger,
                "click_material_item.onclick_fallback",
                match.text,
                None,
                lambda: page.evaluate(
                    """
                    ({ itemId }) => {
                      const el = document.querySelector(`[data-fo-material-id="${itemId}"]`);
                      if (!el) {
                        throw new Error("material nao encontrado para onclick fallback");
                      }
                      const onclick = el.getAttribute("onclick") || "";
                      if (!onclick || !/openMedia4\\s*\\(/i.test(onclick)) {
                        throw new Error("onclick openMedia4 nao encontrado no material");
                      }
                      const runner = new Function("event", onclick);
                      runner.call(el, new MouseEvent("click", { bubbles: true, cancelable: true }));
                    }
                    """,
                    {"itemId": match.item_id},
                ),
            )


def get_material_viewer_state(page: Any) -> ViewerState:
    raw = page.evaluate(
        """
        () => {
          const frame = document.querySelector("#iFrameDocument");
          const aula = document.querySelector("#nomeAula");

          let src = "";
          if (frame) {
            const attrSrc = frame.getAttribute("src") || "";
            let liveSrc = "";
            try {
              liveSrc = frame.contentWindow?.location?.href || "";
            } catch (err) {
              liveSrc = "";
            }
            src = liveSrc || attrSrc || "";
          }

          let lessonName = "";
          if (aula) {
            lessonName =
              aula.value ||
              aula.getAttribute("value") ||
              aula.innerText ||
              aula.textContent ||
              "";
          }

          return {
            lesson_name: (lessonName || "").replace(/\\s+/g, " ").trim(),
            src
          };
        }
        """
    )
    return ViewerState(
        lesson_name=str(raw.get("lesson_name") or ""),
        src=str(raw.get("src") or ""),
    )


def wait_for_material_viewer(
    page: Any,
    config: RuntimeConfig,
    logger: Logger,
    previous_state: ViewerState,
    expected_material_name: str,
) -> ViewerState:
    logged_step(
        logger,
        "wait_for_material_viewer.wait_for_function",
        (
            f"expected_material={expected_material_name} "
            f"previous_name={previous_state.lesson_name[:80]} "
            f"previous_src={previous_state.src[:120]}"
        ),
        config.download_timeout_ms,
        lambda: page.wait_for_function(
            """
            ({ previousName, previousSrc, expectedMaterialName }) => {
              const normalize = (value) => (value || "")
                .normalize("NFKD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .replace(/\\s+/g, " ")
                .trim();
              const frame = document.querySelector("#iFrameDocument");
              const aula = document.querySelector("#nomeAula");
              if (!frame || !aula) return false;

              const attrSrc = frame.getAttribute("src") || "";
              let liveSrc = "";
              try {
                liveSrc = frame.contentWindow?.location?.href || "";
              } catch (err) {
                liveSrc = "";
              }

              const lessonName = (
                aula.value ||
                aula.getAttribute("value") ||
                aula.innerText ||
                aula.textContent ||
                ""
              ).replace(/\\s+/g, " ").trim();
              const src = liveSrc || attrSrc;
              if (!src) return false;
              if (previousSrc && src === previousSrc) return false;

              const lessonNameNorm = normalize(lessonName);
              const expectedNorm = normalize(expectedMaterialName);
              if (!lessonNameNorm || !expectedNorm) return false;
              if (lessonNameNorm !== expectedNorm && !lessonNameNorm.includes(expectedNorm)) return false;
              if (previousName && normalize(previousName) === lessonNameNorm) return false;

              return src.startsWith("blob:") || src.startsWith("http://") || src.startsWith("https://");
            }
            """,
            {
                "previousName": previous_state.lesson_name,
                "previousSrc": previous_state.src,
                "expectedMaterialName": expected_material_name,
            },
            timeout=config.download_timeout_ms,
        ),
    )
    return get_material_viewer_state(page)


def capture_pdf_bytes_from_url(page: Any, pdf_url: str) -> tuple[bytes, str]:
    payload = page.evaluate(
        """
        async ({ pdfUrl }) => {
          if (pdfUrl.startsWith("javascript:")) {
            return { ok: false, error: `URL javascript nao suportada: ${pdfUrl}` };
          }
          if (!pdfUrl.startsWith("blob:") && !pdfUrl.startsWith("http://") && !pdfUrl.startsWith("https://")) {
            return { ok: false, error: `URL nao suportada: ${pdfUrl}` };
          }

          const response = await fetch(pdfUrl, { credentials: "include" });
          if (!response.ok) {
            return { ok: false, error: `fetch falhou com status ${response.status}`, src: pdfUrl };
          }

          const buffer = await response.arrayBuffer();
          const bytes = new Uint8Array(buffer);
          let binary = "";
          const chunkSize = 0x8000;
          for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
          }

          return {
            ok: true,
            src: pdfUrl,
            base64: btoa(binary),
          };
        }
        """,
        {"pdfUrl": pdf_url},
    )

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "Falha ao capturar PDF por URL.")

    return base64.b64decode(payload["base64"]), str(payload["src"])


def capture_pdf_bytes_from_viewer(page: Any) -> tuple[bytes, str]:
    payload = page.evaluate(
        """
        async () => {
          const frame = document.querySelector("#iFrameDocument");
          if (!frame) {
            return { ok: false, error: "iframe #iFrameDocument nao encontrado" };
          }

          let src = frame.getAttribute("src") || "";
          try {
            const liveSrc = frame.contentWindow?.location?.href || "";
            if ((!src || src.startsWith("javascript:")) && liveSrc) {
              src = liveSrc;
            }
          } catch (err) {
            // Ignore cross-context access failures and keep attr src.
          }

          if (!src) {
            return { ok: false, error: "iframe sem src apos abrir o material" };
          }
          if (src.startsWith("javascript:")) {
            return { ok: false, error: `iframe ainda aponta para javascript: ${src}` };
          }
          if (!src.startsWith("blob:") && !src.startsWith("http://") && !src.startsWith("https://")) {
            return { ok: false, error: `origem do viewer nao suportada: ${src}` };
          }

          const response = await fetch(src, { credentials: "include" });
          if (!response.ok) {
            return { ok: false, error: `fetch do viewer falhou com status ${response.status}`, src };
          }

          const buffer = await response.arrayBuffer();
          const bytes = new Uint8Array(buffer);
          let binary = "";
          const chunkSize = 0x8000;
          for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
          }

          return {
            ok: true,
            src,
            base64: btoa(binary),
          };
        }
        """
    )

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "Falha ao capturar bytes do PDF no viewer.")

    return base64.b64decode(payload["base64"]), str(payload["src"])


def collect_clickable_candidates(page: Any) -> list[Candidate]:
    raw = page.evaluate(
        """
        () => {
          const selectors = [
            "a[href]",
            "button",
            "[role='button']",
            "summary",
            "label",
            "h1",
            "h2",
            "h3",
            "h4",
            "li",
            "article",
            "div"
          ];

          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return (
              style &&
              style.visibility !== "hidden" &&
              style.display !== "none" &&
              rect.width > 0 &&
              rect.height > 0
            );
          };

          const norm = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const out = [];
          let serial = 0;

          for (const el of document.querySelectorAll(selectors.join(","))) {
            if (!visible(el)) continue;
            const text = norm(el.innerText || el.textContent || "");
            if (!text || text.length > 400) continue;
            const href = el.href || el.getAttribute("href") || "";
            const tag = (el.tagName || "").toLowerCase();
            const nodeId = `fo-sync-${serial++}`;
            el.setAttribute("data-fo-sync-id", nodeId);
            out.push({ node_id: nodeId, text, href, tag });
          }
          return out;
        }
        """
    )
    return [Candidate(**item) for item in raw]


def score_candidate(
    candidate: Candidate,
    required_terms: list[str],
    preferred_terms: list[str] | None = None,
) -> int:
    text = candidate.searchable_text
    if any(term not in text for term in required_terms):
        return -1

    score = 0
    for term in required_terms:
        if term in text:
            score += 100
            if candidate.text and term in normalize_text(candidate.text):
                score += 25

    for term in preferred_terms or []:
        if term and term in text:
            score += 20

    if candidate.href:
        score += 10
        if ".pdf" in normalize_text(candidate.href):
            score += 50

    if candidate.tag in {"a", "button", "summary"}:
        score += 10

    return score


def select_best_candidate(
    candidates: list[Candidate],
    required_terms: list[str],
    preferred_terms: list[str] | None = None,
) -> Candidate | None:
    best: Candidate | None = None
    best_score = -1
    for candidate in candidates:
        score = score_candidate(candidate, required_terms, preferred_terms)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def click_candidate(page: Any, candidate: Candidate) -> None:
    locator = page.locator(f"[data-fo-sync-id='{candidate.node_id}']").first
    locator.scroll_into_view_if_needed(timeout=5_000)
    locator.click(timeout=5_000)


def extract_pdf_urls(page: Any) -> list[str]:
    values = page.evaluate(
        """
        () => {
          const out = [];
          if (window.location && window.location.href) {
            out.push(window.location.href);
          }

          for (const el of document.querySelectorAll("iframe[src], embed[src], object[data], a[href]")) {
            const value = el.getAttribute("src") || el.getAttribute("data") || el.getAttribute("href");
            if (!value) continue;
            try {
              out.push(new URL(value, document.baseURI).href);
            } catch (err) {
              // Ignore malformed URLs from the portal.
            }
          }
          return out;
        }
        """
    )

    seen: set[str] = set()
    ordered: list[str] = []
    for url in values:
        if not isinstance(url, str):
            continue
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def page_identity_is_consistent(page: Any, target: MaterialTarget, source_label: str) -> bool:
    body_text = ""
    try:
        body_text = normalize_text(page.locator("body").inner_text(timeout=3_000))
    except Exception:
        body_text = ""

    combined = normalize_text(f"{page.url} {page.title()} {source_label} {body_text}")
    return target.normalized_subject in combined and target.normalized_type in combined


def fetch_pdf_from_url(
    page: Any,
    pdf_url: str,
    destination: Path,
    referer: str,
) -> bool:
    _ = referer
    if pdf_url.startswith("javascript:"):
        return False
    try:
        raw_bytes, _source_url = capture_pdf_bytes_from_url(page, pdf_url)
    except Exception:
        return False
    if not raw_bytes.startswith(b"%PDF-"):
        return False
    destination.write_bytes(raw_bytes)
    return True


def material_duplicate_conflicts(
    hashes_by_sha: dict[str, str],
    sha: str,
    target: MaterialTarget,
) -> str | None:
    existing_target = hashes_by_sha.get(sha)
    if existing_target and existing_target != target.target_key:
        return existing_target
    hashes_by_sha[sha] = target.target_key
    return None


def download_target_pdf(
    context: Any,
    config: RuntimeConfig,
    logger: Logger,
    target: MaterialTarget,
    target_dir: Path,
    raw_root: Path,
    hashes_by_sha: dict[str, str],
) -> DownloadResult:
    page = open_authenticated_course_page(context, config, logger)
    try:
        logger.log(f"[{target.target_key}] etapa=abrir_curso.inicio")
        previous_material_state = get_material_viewer_state(page)
        logger.log(
            f"[{target.target_key}] etapa=abrir_curso.ok "
            f"viewer_name_inicial={previous_material_state.lesson_name[:120]} "
            f"viewer_src_inicial={previous_material_state.src[:240]}"
        )
        logger.log(f"[{target.target_key}] etapa=navegar_arvore.inicio")
        path_match = ensure_tree_path_open(page, logger, target)
        logger.log(f"[{target.target_key}] etapa=navegar_arvore.ok card_final={path_match.text}")
        logger.log(f"[{target.target_key}] etapa=inspecionar_body_final.inicio")
        log_card_body_probe(page, logger, target, path_match.card_id)
        logger.log(f"[{target.target_key}] etapa=inspecionar_body_final.ok")
        logger.log(f"[{target.target_key}] etapa=localizar_material.inicio")
        material_match = find_material_item(page, path_match.card_id, target.material_type)
        if material_match is None:
            raise RuntimeError("Material nao localizado apos abrir a disciplina/obra.")
        logger.log(f"[{target.target_key}] etapa=localizar_material.ok material={material_match.text}")

        source_label = f"{' > '.join(target.portal_path)} -> {material_match.text}"
        logger.log(f"[{target.target_key}] abrindo material: {material_match.text}")
        logger.log(f"[{target.target_key}] etapa=abrir_material.inicio")
        neutralize_intro_overlay(page, logger)
        click_material_item(page, material_match, logger)
        current_material_state = wait_for_material_viewer(
            page,
            config,
            logger,
            previous_material_state,
            target.material_type,
        )
        logger.log(
            f"[{target.target_key}] viewer atualizado nome={current_material_state.lesson_name} "
            f"src={current_material_state.src[:240]}"
        )
        logger.log(f"[{target.target_key}] etapa=abrir_material.ok")

        destination = target_dir / target.filename
        temp_destination = destination.with_suffix(".tmp")
        if temp_destination.exists():
            temp_destination.unlink()

        logger.log(f"[{target.target_key}] etapa=capturar_viewer_blob.inicio")
        if not resolve_pdf_from_page(page, source_label, temp_destination, config, target):
            raise RuntimeError("Material abriu, mas o PDF correto nao ficou acessivel.")
        logger.log(f"[{target.target_key}] etapa=capturar_viewer_blob.ok")

        if not is_pdf_file(temp_destination):
            raise RuntimeError("Arquivo baixado nao possui cabecalho PDF valido.")

        logger.log(f"[{target.target_key}] etapa=salvar_arquivo.inicio path={destination}")
        sha = sha256_file(temp_destination)
        duplicate_of = material_duplicate_conflicts(hashes_by_sha, sha, target)
        if duplicate_of:
            temp_destination.unlink(missing_ok=True)
            raise RuntimeError(
                "Hash duplicado com outro alvo ja salvo "
                f"({duplicate_of}); pulando para evitar nomear PDF errado."
            )

        temp_destination.rename(destination)
        logger.log(f"[{target.target_key}] etapa=salvar_arquivo.ok sha256={sha[:16]} bytes={destination.stat().st_size}")
        return DownloadResult(
            source_url=getattr(page, "_fo_sync_source_url", page.url),
            source_label=source_label,
            sha256=sha,
            byte_size=destination.stat().st_size,
            file_name=destination.name,
            relative_path=str(destination.relative_to(raw_root)),
        )
    finally:
        try:
            page.close()
        except Exception:
            pass


def open_candidate(page: Any, candidate: Candidate, config: RuntimeConfig) -> Any:
    context = page.context
    existing_pages = list(context.pages)

    try:
        with page.expect_download(timeout=config.download_timeout_ms) as download_info:
            click_candidate(page, candidate)
        download = download_info.value
        holder_page = context.new_page()
        holder_page.set_default_timeout(config.nav_timeout_ms)
        download_path = Path(tempfile.mkstemp(prefix="fo-sync-download-", suffix=".pdf")[1])
        download.save_as(str(download_path))
        holder_page.goto(f"file://{download_path}", wait_until="domcontentloaded")
        holder_page._fo_sync_download_path = download_path  # type: ignore[attr-defined]
        holder_page._fo_sync_source_url = candidate.href or page.url  # type: ignore[attr-defined]
        return holder_page
    except PlaywrightTimeoutError:
        page.wait_for_timeout(2_000)

    new_pages = [item for item in context.pages if item not in existing_pages]
    if new_pages:
        popup = new_pages[-1]
        popup.set_default_timeout(config.nav_timeout_ms)
        popup.wait_for_load_state("domcontentloaded", timeout=config.nav_timeout_ms)
        try:
            popup.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        return popup

    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass
    return page


def resolve_pdf_from_page(
    page: Any,
    source_label: str,
    destination: Path,
    config: RuntimeConfig,
    target: MaterialTarget,
) -> bool:
    if not page_identity_is_consistent(page, target, source_label):
        return False

    try:
        raw_bytes, source_url = capture_pdf_bytes_from_viewer(page)
        if raw_bytes.startswith(b"%PDF-"):
            destination.write_bytes(raw_bytes)
            page._fo_sync_source_url = source_url  # type: ignore[attr-defined]
            return True
    except Exception:
        pass

    for pdf_url in extract_pdf_urls(page):
        if fetch_pdf_from_url(page, pdf_url, destination, referer=page.url):
            page._fo_sync_source_url = pdf_url  # type: ignore[attr-defined]
            return True
    return False


def write_manifest(raw_dir: Path, entries: list[ManifestEntry]) -> None:
    manifest_json = raw_dir / "manifest.json"
    manifest_tsv = raw_dir / "manifest.tsv"

    manifest_payload = {
        "schema_version": MATERIAL_MANIFEST_SCHEMA_VERSION,
        "generated_at": now_utc_iso(),
        "count": len(entries),
        "success_count": sum(1 for entry in entries if entry.status == "ok"),
        "error_count": sum(1 for entry in entries if entry.status != "ok"),
        "items": [asdict(entry) for entry in entries],
    }
    validate_material_manifest_payload(manifest_payload)
    manifest_json.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    columns = [
        "target_key",
        "group",
        "portal_subject",
        "output_subject",
        "material_type",
        "status",
        "file_name",
        "relative_path",
        "sha256",
        "byte_size",
        "source_url",
        "source_label",
        "started_at",
        "finished_at",
        "error",
    ]
    lines = ["\t".join(columns)]
    for entry in entries:
        payload = asdict(entry)
        row = [str(payload[column]).replace("\t", " ").replace("\n", " ") for column in columns]
        lines.append("\t".join(row))
    manifest_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")


def promote_raw_dir(state_root: Path, temp_raw_dir: Path) -> None:
    raw_dir = state_root / "raw"
    backup_dir = state_root / ".raw_previous"

    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if raw_dir.exists():
        raw_dir.rename(backup_dir)
    temp_raw_dir.rename(raw_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


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
    logger.log("Inicio da sincronizacao de materiais do Federal Online.")
    logger.log(f"Arquivo de configuracao: {config.env_file}")
    logger.log(f"Diretorio de estado: {config.state_root}")
    logger.log(f"Log persistente: {config.persistent_log_file}")

    temp_raw_dir = config.state_root / f".raw_build_{run_stamp}_{os.getpid()}"
    temp_files_dir = temp_raw_dir / "files"
    temp_logs_dir = temp_raw_dir / "logs"
    temp_files_dir.mkdir(parents=True, exist_ok=True)
    temp_logs_dir.mkdir(parents=True, exist_ok=True)
    logger.add_log_file(temp_logs_dir / "fo_sync_materials.log")
    logger.log(f"Log temporario da execucao: {temp_logs_dir / 'fo_sync_materials.log'}")

    entries: list[ManifestEntry] = []
    hashes_by_sha: dict[str, str] = {}

    browser = None
    playwright = None
    try:
        targets = build_targets()
        if args.only_subject:
            targets = [target for target in targets if target.portal_subject == args.only_subject]
        if args.only_type:
            targets = [target for target in targets if target.material_type == args.only_type]
        if not targets:
            raise RuntimeError("Nenhum alvo selecionado para execucao com os filtros atuais.")
        logger.log(
            "Alvos selecionados: "
            f"{len(targets)} | filtro_subject={args.only_subject or '-'} | filtro_type={args.only_type or '-'}"
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
        seed_page = open_authenticated_course_page(context, config, logger)
        save_storage_state(context, config.storage_state_file)
        logger.log(f"storage_state persistido apos seed em {config.storage_state_file}")
        seed_page.close()

        for target in targets:
            started_at = now_utc_iso()
            logger.log(f"[{target.target_key}] iniciando.")
            try:
                result = download_target_pdf(
                    context=context,
                    config=config,
                    logger=logger,
                    target=target,
                    target_dir=temp_files_dir,
                    raw_root=temp_raw_dir,
                    hashes_by_sha=hashes_by_sha,
                )
                logger.log(
                    f"[{target.target_key}] OK -> {result.relative_path} "
                    f"(sha256={result.sha256[:12]}..., bytes={result.byte_size})"
                )
                entries.append(
                    ManifestEntry(
                        target_key=target.target_key,
                        group=target.group,
                        portal_subject=target.portal_subject,
                        output_subject=target.output_subject,
                        material_type=target.material_type,
                        status="ok",
                        file_name=result.file_name,
                        relative_path=result.relative_path,
                        sha256=result.sha256,
                        byte_size=result.byte_size,
                        source_url=result.source_url,
                        source_label=result.source_label,
                        started_at=started_at,
                        finished_at=now_utc_iso(),
                        error="",
                    )
                )
            except Exception as exc:
                logger.log(f"[{target.target_key}] ERRO -> {exc}")
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
                        error=str(exc),
                    )
                )

        write_manifest(temp_raw_dir, entries)
        logger.log("Manifest gravado com sucesso.")
        context.close()
        logger.log(
            "Resumo: "
            f"{sum(1 for entry in entries if entry.status == 'ok')} OK, "
            f"{sum(1 for entry in entries if entry.status != 'ok')} erro(s)."
        )
        logger.log("etapa=promover_raw.inicio")
        temp_run_log = temp_logs_dir / "fo_sync_materials.log"
        promote_raw_dir(config.state_root, temp_raw_dir)
        logger.remove_log_file(temp_run_log)
        logger.add_log_file(config.raw_dir / "logs" / "fo_sync_materials.log")
        logger.log("etapa=promover_raw.ok")
        return 0
    except Exception as exc:
        logger.log(f"Falha fatal da sincronizacao: {exc}")
        logger.log(traceback.format_exc())
        write_manifest(temp_raw_dir, entries)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
