#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fo_sync_materials import (
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_ENV_FILE,
    DEFAULT_NAV_TIMEOUT_MS,
    DEFAULT_STATE_ROOT,
    Logger,
    build_runtime_config,
    neutralize_intro_overlay,
    normalize_text,
    open_authenticated_course_page,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


PROBE_PATH = ["Aulas Gerais", "Biologia", "Biologia I"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe minimo da arvore lateral do Federal Online."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Arquivo env. Padrao: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=f"Diretorio de estado. Padrao: {DEFAULT_STATE_ROOT}",
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
        help=f"Timeout de navegacao. Padrao: {DEFAULT_NAV_TIMEOUT_MS}",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Any:
    runtime_args = SimpleNamespace(
        env_file=args.env_file,
        state_root=args.state_root,
        headed=args.headed,
        nav_timeout_ms=args.nav_timeout_ms,
        download_timeout_ms=DEFAULT_DOWNLOAD_TIMEOUT_MS,
    )
    return build_runtime_config(runtime_args)


def resolve_scope_selector(card: dict[str, Any]) -> tuple[str | None, str | None]:
    if card.get("data_target"):
        return str(card["data_target"]), "data_target"
    if card.get("body_id"):
        return f"#{card['body_id']}", "body_id"
    return None, None


def snapshot_cards(page: Any, scope_selector: str | None = None) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        ({ scopeSelector }) => {
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
          const root = scopeSelector
            ? document.querySelector(scopeSelector)
            : document;
          if (!root) return [];

          const selector = scopeSelector ? ".card .header-wrapper" : ".header-wrapper";
          const results = [];

          for (const header of root.querySelectorAll(selector)) {
            if (!visible(header)) continue;
            const card = header.closest(".card") || header.parentElement;
            if (!card) continue;

            const body =
              card.querySelector(":scope > .card-body, :scope > .collapse, :scope > .accordion-collapse") ||
              Array.from(card.children).find((el) =>
                (el.classList && (el.classList.contains("card-body") || el.classList.contains("collapse") || el.classList.contains("accordion-collapse")))
              ) ||
              card.querySelector(".card-body, .collapse, .accordion-collapse");

            const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
            const cardId = `fo-probe-card-${stamp}`;
            card.setAttribute("data-fo-probe-card-id", cardId);

            let bodyId = "";
            let bodyClass = "";
            if (body) {
              bodyId = body.id || "";
              bodyClass = body.className || "";
            }

            results.push({
              card_id: cardId,
              title: (header.innerText || header.textContent || "").replace(/\\s+/g, " ").trim(),
              normalized_title: normalize(header.innerText || header.textContent || ""),
              data_target: header.getAttribute("data-target") || header.getAttribute("href") || "",
              body_id: bodyId,
              body_class: bodyClass,
            });
          }
          return results;
        }
        """,
        {"scopeSelector": scope_selector},
    )


def log_scope(logger: Logger, label: str, path: list[str], scope_selector: str | None, cards: list[dict[str, Any]]) -> None:
    logger.log(f"[{label}] path_atual={' > '.join(path) if path else '(raiz)'}")
    logger.log(f"[{label}] scope_atual={scope_selector or 'document'}")
    logger.log(f"[{label}] cards_visiveis={json.dumps(cards, ensure_ascii=False)}")
    logger.log(
        f"[{label}] titulos_normalizados={json.dumps([card['normalized_title'] for card in cards], ensure_ascii=False)}"
    )


def find_card_in_scope(cards: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    target = normalize_text(title)
    exact = [card for card in cards if card["normalized_title"] == target]
    if exact:
        return exact[0]
    contains = [card for card in cards if target in card["normalized_title"]]
    return contains[0] if contains else None


def click_card(page: Any, logger: Logger, card: dict[str, Any]) -> None:
    selector = f"[data-fo-probe-card-id='{card['card_id']}'] .header-wrapper"
    header = page.locator(selector).first
    logger.log(f"[click_card] tentando click card={card['title']} selector={selector}")
    try:
        header.scroll_into_view_if_needed(timeout=5_000)
        header.click(timeout=5_000)
    except Exception as exc:
        logger.log(f"[click_card] click normal falhou card={card['title']} exc={type(exc).__name__}: {exc}")
        page.evaluate(
            """
            ({ cardId }) => {
              const el = document.querySelector(`[data-fo-probe-card-id="${cardId}"] .header-wrapper`);
              if (!el) throw new Error("header-wrapper nao encontrado no fallback JS");
              el.click();
            }
            """,
            {"cardId": card["card_id"]},
        )
    page.wait_for_timeout(800)


def probe_step(page: Any, logger: Logger, current_path: list[str], scope_selector: str | None, expected_title: str) -> tuple[dict[str, Any], str | None]:
    neutralize_intro_overlay(page, logger)
    before_cards = snapshot_cards(page, scope_selector)
    log_scope(logger, f"antes::{expected_title}", current_path, scope_selector, before_cards)

    chosen = find_card_in_scope(before_cards, expected_title)
    if not chosen:
        logger.log(
            f"[falha::{expected_title}] titulo_nao_encontrado expected={expected_title} "
            f"disponiveis={json.dumps([card['title'] for card in before_cards], ensure_ascii=False)}"
        )
        raise RuntimeError(f"Titulo nao encontrado no scope atual: {expected_title}")

    logger.log(
        f"[match::{expected_title}] escolhido={json.dumps(chosen, ensure_ascii=False)}"
    )

    if "show" not in (chosen.get("body_class") or ""):
        logger.log(f"[toggle::{expected_title}] body fechado, abrindo card")
        click_card(page, logger, chosen)
        neutralize_intro_overlay(page, logger)
    else:
        logger.log(f"[toggle::{expected_title}] body ja aberto, nao clicar")

    current_path = [*current_path, expected_title]
    next_scope_selector, scope_origin = resolve_scope_selector(chosen)
    after_cards = snapshot_cards(page, next_scope_selector)
    logger.log(
        f"[depois::{expected_title}] scope_atual_novo={next_scope_selector or '(vazio)'} "
        f"origem_scope={scope_origin or 'none'}"
    )
    log_scope(logger, f"depois::{expected_title}", current_path, next_scope_selector, after_cards)

    refreshed = find_card_in_scope(snapshot_cards(page, scope_selector), expected_title) or chosen
    logger.log(
        f"[estado::{expected_title}] bodyClass_apos={refreshed.get('body_class', '')} dataTarget={refreshed.get('data_target', '')}"
    )
    return refreshed, next_scope_selector


def inspect_final_scope(page: Any, logger: Logger, scope_selector: str | None, path: list[str]) -> None:
    payload = page.evaluate(
        """
        ({ scopeSelector }) => {
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const norm = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const root = scopeSelector ? document.querySelector(scopeSelector) : null;
          if (!root) {
            return {
              exists: false,
              body_html: "",
              body_text: "",
              links_clickaveis: [],
              botoes_visiveis: [],
              anchors_com_href: [],
              elementos_onclick: [],
              material_list_texts: [],
              material_matches: [],
              existence: {}
            };
          }

          const selectorsToCheck = ["#listDocuments", "#nomeAula", "#iFrameDocument", "#btnMaterial"];
          const existence = {};
          for (const selector of selectorsToCheck) {
            existence[selector] = !!root.querySelector(selector) || !!document.querySelector(selector);
          }

          const linksClickaveis = [];
          for (const el of root.querySelectorAll("a, [role='link']")) {
            if (!visible(el)) continue;
            linksClickaveis.push({
              tag: (el.tagName || "").toLowerCase(),
              text: norm(el.innerText || el.textContent || ""),
              href: el.getAttribute("href") || "",
              classes: el.className || ""
            });
          }

          const botoesVisiveis = [];
          for (const el of root.querySelectorAll("button, [role='button']")) {
            if (!visible(el)) continue;
            botoesVisiveis.push({
              tag: (el.tagName || "").toLowerCase(),
              text: norm(el.innerText || el.textContent || ""),
              id: el.id || "",
              classes: el.className || "",
              type: el.getAttribute("type") || ""
            });
          }

          const anchorsComHref = [];
          for (const el of root.querySelectorAll("a[href]")) {
            if (!visible(el)) continue;
            anchorsComHref.push({
              text: norm(el.innerText || el.textContent || ""),
              href: el.getAttribute("href") || "",
              classes: el.className || ""
            });
          }

          const elementosOnclick = [];
          for (const el of root.querySelectorAll("[onclick]")) {
            if (!visible(el)) continue;
            elementosOnclick.push({
              tag: (el.tagName || "").toLowerCase(),
              text: norm(el.innerText || el.textContent || ""),
              onclick: el.getAttribute("onclick") || "",
              classes: el.className || ""
            });
          }

          const materialNeedles = [
            "Resumo",
            "Lista de exercícios",
            "Lista de exercicios",
            "Gabarito comentado"
          ].map((value) => value.toLowerCase());
          const materialListTexts = [];
          const materialMatches = [];
          for (const el of root.querySelectorAll("li, a, button, [role='button'], [onclick], .item, .material, .material-item, .panel-item, div, span")) {
            if (!visible(el)) continue;
            const text = norm(el.innerText || el.textContent || "");
            if (!text || text.length > 500) continue;
            if (
              text.toLowerCase().includes("resumo") ||
              text.toLowerCase().includes("lista de exercícios") ||
              text.toLowerCase().includes("lista de exercicios") ||
              text.toLowerCase().includes("gabarito comentado")
            ) {
              materialListTexts.push(text);
              materialMatches.push({
                tag: (el.tagName || "").toLowerCase(),
                text,
                id: el.id || "",
                classes: el.className || "",
                href: el.getAttribute("href") || "",
                onclick: el.getAttribute("onclick") || ""
              });
            }
          }

          return {
            exists: true,
            body_html: root.innerHTML || "",
            body_text: root.innerText || root.textContent || "",
            links_clickaveis: linksClickaveis,
            botoes_visiveis: botoesVisiveis,
            anchors_com_href: anchorsComHref,
            elementos_onclick: elementosOnclick,
            material_list_texts: materialListTexts,
            material_matches: materialMatches,
            existence
          };
        }
        """,
        {"scopeSelector": scope_selector},
    )

    logger.log(f"[final_body] path={' > '.join(path)}")
    logger.log(f"[final_body] scope={scope_selector or '(vazio)'} exists={payload['exists']}")
    logger.log(f"[final_body] existence={json.dumps(payload['existence'], ensure_ascii=False)}")
    logger.log(f"[final_body] body_text={json.dumps(payload['body_text'][:4000], ensure_ascii=False)}")
    logger.log(f"[final_body] body_html={json.dumps(payload['body_html'][:4000], ensure_ascii=False)}")
    logger.log(f"[final_body] links_clickaveis={json.dumps(payload['links_clickaveis'], ensure_ascii=False)}")
    logger.log(f"[final_body] botoes_visiveis={json.dumps(payload['botoes_visiveis'], ensure_ascii=False)}")
    logger.log(f"[final_body] anchors_com_href={json.dumps(payload['anchors_com_href'], ensure_ascii=False)}")
    logger.log(f"[final_body] elementos_onclick={json.dumps(payload['elementos_onclick'], ensure_ascii=False)}")
    logger.log(f"[final_body] material_list_texts={json.dumps(payload['material_list_texts'], ensure_ascii=False)}")
    logger.log(f"[final_body] material_matches={json.dumps(payload['material_matches'], ensure_ascii=False)}")


def main() -> int:
    args = parse_args()
    try:
        config = build_config(args)
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
    probe_log = config.persistent_logs_dir / "fo_probe_tree.latest.log"
    probe_log.write_text("", encoding="utf-8")
    logger = Logger([probe_log])

    logger.log("Inicio do probe da arvore lateral do Federal Online.")
    logger.log(f"Curso alvo: {config.course_url}")
    logger.log(f"Path alvo: {' > '.join(PROBE_PATH)}")

    browser = None
    playwright = None
    context = None
    page = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=not args.headed)

        context_kwargs: dict[str, Any] = {}
        if config.storage_state_file.exists():
            context_kwargs["storage_state"] = str(config.storage_state_file)
            logger.log(f"Carregando storage_state existente: {config.storage_state_file}")
        else:
            logger.log("Nenhum storage_state encontrado; probe segue sem sessao persistida.")

        context = browser.new_context(**context_kwargs)
        page = open_authenticated_course_page(context, config, logger)
        context.storage_state(path=str(config.storage_state_file))
        logger.log(f"storage_state persistido apos abrir curso: {config.storage_state_file}")

        initial_cards = snapshot_cards(page, None)
        log_scope(logger, "estado_inicial", [], None, initial_cards)

        current_scope: str | None = None
        current_path: list[str] = []
        for expected_title in PROBE_PATH:
            chosen, current_scope = probe_step(page, logger, current_path, current_scope, expected_title)
            current_path.append(expected_title)

        inspect_final_scope(page, logger, current_scope, current_path)
        logger.log("Probe concluido com sucesso.")
        return 0
    except Exception as exc:
        logger.log(f"Probe falhou: {exc}")
        logger.log(traceback.format_exc())
        return 1
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
