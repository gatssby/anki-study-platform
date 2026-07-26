#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fo_session_security import protect_storage_state, save_storage_state
from fo_sync_materials import (
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_ENV_FILE,
    DEFAULT_NAV_TIMEOUT_MS,
    DEFAULT_STATE_ROOT,
    Logger,
    build_runtime_config,
    neutralize_intro_overlay,
    normalize_text,
    now_utc_iso,
    open_authenticated_course_page,
    summarize_exception,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - handled at runtime on the VPS.
    sync_playwright = None


PROBE_PATH = ["Aulas Gerais", "Biologia", "Biologia I"]
RELEVANT_NETWORK_NEEDLES = (
    "aula",
    "video",
    "media",
    "curso-aula",
    "vimeo",
    "m3u8",
    "mp4",
    "wistia",
    "player",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe de nomes e duracoes de aulas do Federal Online."
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
        help=f"Timeout de navegacao em ms. Padrao: {DEFAULT_NAV_TIMEOUT_MS}",
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
          const root = scopeSelector ? document.querySelector(scopeSelector) : document;
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
            const cardId = `fo-probe-aulas-card-${stamp}`;
            card.setAttribute("data-fo-probe-aulas-card-id", cardId);

            results.push({
              card_id: cardId,
              title: (header.innerText || header.textContent || "").replace(/\\s+/g, " ").trim(),
              normalized_title: normalize(header.innerText || header.textContent || ""),
              data_target: header.getAttribute("data-target") || header.getAttribute("href") || "",
              body_id: body ? (body.id || "") : "",
              body_class: body ? (body.className || "") : "",
            });
          }
          return results;
        }
        """,
        {"scopeSelector": scope_selector},
    )


def resolve_scope_selector(card: dict[str, Any]) -> str | None:
    if card.get("data_target"):
        return str(card["data_target"])
    if card.get("body_id"):
        return f"#{card['body_id']}"
    return None


def find_card(cards: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    target = normalize_text(title)
    exact = [card for card in cards if card["normalized_title"] == target]
    if exact:
        return exact[0]
    contains = [card for card in cards if target in card["normalized_title"]]
    return contains[0] if contains else None


def click_card(page: Any, logger: Logger, card: dict[str, Any]) -> None:
    selector = f"[data-fo-probe-aulas-card-id='{card['card_id']}'] .header-wrapper"
    header = page.locator(selector).first
    try:
        header.scroll_into_view_if_needed(timeout=5_000)
        header.click(timeout=5_000)
    except Exception as exc:
        logger.log(f"[tree] click normal falhou card={card['title']} detalhe={summarize_exception(exc)}")
        page.evaluate(
            """
            ({ cardId }) => {
              const el = document.querySelector(`[data-fo-probe-aulas-card-id="${cardId}"] .header-wrapper`);
              if (!el) throw new Error("header-wrapper nao encontrado no fallback JS");
              el.click();
            }
            """,
            {"cardId": card["card_id"]},
        )
    page.wait_for_timeout(800)


def navigate_probe_path(page: Any, logger: Logger) -> str:
    scope_selector: str | None = None
    current_path: list[str] = []
    for expected_title in PROBE_PATH:
        neutralize_intro_overlay(page, logger)
        cards = snapshot_cards(page, scope_selector)
        logger.log(
            f"[tree] path={' > '.join(current_path) or '(raiz)'} scope={scope_selector or 'document'} "
            f"cards={json.dumps([card['title'] for card in cards], ensure_ascii=False)}"
        )
        chosen = find_card(cards, expected_title)
        if not chosen:
            raise RuntimeError(f"Titulo nao encontrado no scope atual: {expected_title}")
        if "show" not in (chosen.get("body_class") or ""):
            logger.log(f"[tree] abrindo card={expected_title}")
            click_card(page, logger, chosen)
            neutralize_intro_overlay(page, logger)
        else:
            logger.log(f"[tree] card ja aberto={expected_title}")
        current_path.append(expected_title)
        scope_selector = resolve_scope_selector(chosen)
        logger.log(f"[tree] novo_scope={scope_selector or '(vazio)'}")
        if not scope_selector:
            raise RuntimeError(f"Scope final ausente apos abrir: {expected_title}")
    return scope_selector


def collect_video_lessons(page: Any, scope_selector: str, logger: Logger) -> list[dict[str, Any]]:
    lessons = page.evaluate(
        """
        ({ scopeSelector }) => {
          const root = document.querySelector(scopeSelector);
          if (!root) return [];
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const parseOnclick = (onclick) => {
            const match = onclick.match(/openMedia4\\s*\\(\\s*event\\s*,\\s*['"]([^'"]+)['"]\\s*,\\s*['"]([^'"]+)['"]\\s*,\\s*['"]([^'"]+)['"]\\s*\\)/);
            if (!match) return null;
            return { item_id: match[1], title: match[2], media_type: match[3] };
          };
          const out = [];
          let position = 0;
          for (const el of root.querySelectorAll("[onclick]")) {
            const onclick = el.getAttribute("onclick") || "";
            const parsed = parseOnclick(onclick);
            if (!parsed || parsed.media_type !== "V") continue;
            const probeId = `fo-probe-aula-video-${Date.now()}-${Math.random().toString(36).slice(2, 10)}-${position}`;
            el.setAttribute("data-fo-probe-aula-video-id", probeId);
            const orderMatch = parsed.title.match(/\\bAula\\s*(\\d+)/i);
            out.push({
              order: orderMatch ? Number(orderMatch[1]) : position + 1,
              position,
              nome_aula: parsed.title.replace(/^\\s*Aula\\s*\\d+\\s*[-–—:]\\s*/i, "").trim() || parsed.title,
              titulo_original: parsed.title,
              item_id: parsed.item_id,
              media_type: parsed.media_type,
              onclick,
              text: clean(el.innerText || el.textContent || ""),
              tag: (el.tagName || "").toLowerCase(),
              id: el.id || "",
              classes: el.className || "",
              visible: visible(el),
              probe_id: probeId,
            });
            position += 1;
          }
          return out;
        }
        """,
        {"scopeSelector": scope_selector},
    )
    logger.log(f"[aulas] total_video_lessons={len(lessons)}")
    logger.log(f"[aulas] lessons={json.dumps(lessons, ensure_ascii=False)[:12000]}")
    return lessons


def is_relevant_network_url(url: str) -> bool:
    lowered = url.lower()
    return any(needle in lowered for needle in RELEVANT_NETWORK_NEEDLES)


def attach_network_capture(page: Any, network_events: list[dict[str, Any]]) -> None:
    def on_request(request: Any) -> None:
        if not is_relevant_network_url(request.url):
            return
        network_events.append(
            {
                "kind": "request",
                "ts": now_utc_iso(),
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "post_data": (request.post_data or "")[:1000],
            }
        )

    def on_response(response: Any) -> None:
        if not is_relevant_network_url(response.url):
            return
        headers = {}
        try:
            headers = response.headers
        except Exception:
            pass
        event: dict[str, Any] = {
            "kind": "response",
            "ts": now_utc_iso(),
            "url": response.url,
            "status": response.status,
            "content_type": headers.get("content-type", ""),
        }
        content_type = str(event["content_type"]).lower()
        if any(kind in content_type for kind in ("json", "text", "javascript")):
            try:
                event["body_snippet"] = response.text()[:3000]
            except Exception:
                event["body_snippet_error"] = "unavailable"
        network_events.append(event)

    page.on("request", on_request)
    page.on("response", on_response)


def click_first_video_lesson(page: Any, lesson: dict[str, Any], logger: Logger) -> None:
    selector = f"[data-fo-probe-aula-video-id='{lesson['probe_id']}']"
    logger.log(f"[aula_click] abrindo_primeira_aula selector={selector} titulo={lesson['titulo_original']}")
    locator = page.locator(selector).first
    try:
        locator.scroll_into_view_if_needed(timeout=5_000)
        locator.click(timeout=5_000)
    except Exception as exc:
        logger.log(f"[aula_click] click normal falhou; usando fallback JS detalhe={summarize_exception(exc)}")
        page.evaluate(
            """
            ({ probeId }) => {
              const el = document.querySelector(`[data-fo-probe-aula-video-id="${probeId}"]`);
              if (!el) throw new Error("aula de video nao encontrada pelo probe_id");
              el.click();
            }
            """,
            {"probeId": lesson["probe_id"]},
        )


def collect_duration_diagnostics(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const visible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const attrs = (el) => {
            const out = {};
            for (const attr of Array.from(el.attributes || [])) {
              const name = attr.name || "";
              const value = attr.value || "";
              if (/tempo|time|duration|duracao|dura|video|media|aula|id/i.test(name + " " + value)) {
                out[name] = value.slice(0, 500);
              }
            }
            return out;
          };
          const durationPattern = /\\b(?:\\d{1,2}:)?\\d{1,2}:\\d{2}\\b/g;
          const bodyText = clean(document.body?.innerText || "");
          const durationTextMatches = Array.from(new Set(bodyText.match(durationPattern) || [])).slice(0, 60);

          const videos = Array.from(document.querySelectorAll("video")).map((video, index) => ({
            index,
            id: video.id || "",
            classes: video.className || "",
            src: video.currentSrc || video.src || "",
            duration: Number.isFinite(video.duration) ? video.duration : null,
            current_time: Number.isFinite(video.currentTime) ? video.currentTime : null,
            ready_state: video.readyState,
            paused: video.paused,
            controls: video.controls,
            attrs: attrs(video),
            html: (video.outerHTML || "").slice(0, 2000),
          }));

          const hiddenInputs = Array.from(document.querySelectorAll("input[type='hidden'], input")).map((input) => ({
            id: input.id || "",
            name: input.getAttribute("name") || "",
            value: input.getAttribute("value") || input.value || "",
            attrs: attrs(input),
          })).filter((item) => /tempo|time|duration|duracao|dura|video|media|aula|id/i.test(`${item.id} ${item.name} ${item.value} ${JSON.stringify(item.attrs)}`)).slice(0, 120);

          const playerElements = Array.from(document.querySelectorAll("video, iframe, [id*='video' i], [class*='video' i], [id*='player' i], [class*='player' i], [id*='tempo' i], [class*='tempo' i], [id*='duration' i], [class*='duration' i], [id*='duracao' i], [class*='duracao' i]")).map((el, index) => ({
            index,
            tag: (el.tagName || "").toLowerCase(),
            id: el.id || "",
            classes: el.className || "",
            text: clean(el.innerText || el.textContent || "").slice(0, 800),
            visible: visible(el),
            attrs: attrs(el),
            src: el.getAttribute("src") || "",
            html: (el.outerHTML || "").slice(0, 2000),
          })).slice(0, 120);

          const scripts = Array.from(document.querySelectorAll("script")).map((script, index) => {
            const text = script.textContent || "";
            if (!/tempo|time|duration|duracao|dura|video|media|aula|player/i.test(text)) return null;
            return {
              index,
              src: script.getAttribute("src") || "",
              text: clean(text).slice(0, 2500),
            };
          }).filter(Boolean).slice(0, 40);

          return {
            url: location.href,
            title: document.title || "",
            duration_text_matches: durationTextMatches,
            videos,
            hidden_inputs: hiddenInputs,
            player_elements: playerElements,
            scripts,
            body_text_snippet: bodyText.slice(0, 6000),
            html_snippet: (document.body?.innerHTML || "").slice(0, 12000),
          };
        }
        """
    )


def wait_for_video_metadata(page: Any, logger: Logger) -> None:
    try:
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll("video"))
              .some((video) => Number.isFinite(video.duration) && video.duration > 0)
            """,
            timeout=10_000,
        )
        logger.log("[duration] video_metadata_disponivel=true")
    except Exception as exc:
        logger.log(f"[duration] video_metadata_disponivel=false detalhe={summarize_exception(exc)}")


def write_probe_json(probe_dir: Path, payload: dict[str, Any]) -> Path:
    probe_dir.mkdir(parents=True, exist_ok=True)
    output = probe_dir / "fo_probe_aulas.latest.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stamped = probe_dir / f"fo_probe_aulas.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    stamped.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


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
    probe_dir = config.state_root / "cronograma_fo_probe"
    probe_log = config.persistent_logs_dir / "fo_probe_aulas.latest.log"
    probe_log.write_text("", encoding="utf-8")
    logger = Logger([probe_log])

    logger.log("Inicio do probe de aulas do Federal Online.")
    logger.log(f"Curso alvo: {config.course_url}")
    logger.log(f"Path alvo: {' > '.join(PROBE_PATH)}")
    logger.log(f"JSON de diagnostico: {probe_dir / 'fo_probe_aulas.latest.json'}")

    browser = None
    playwright = None
    context = None
    page = None
    payload: dict[str, Any] = {
        "generated_at": now_utc_iso(),
        "course_url": config.course_url,
        "probe_path": PROBE_PATH,
        "lessons": [],
        "first_lesson": None,
        "duration_diagnostics": {},
        "network_events": [],
        "status": "started",
        "error": "",
    }

    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=not args.headed)

        context_kwargs: dict[str, Any] = {}
        protect_storage_state(config.storage_state_file)
        if config.storage_state_file.exists():
            context_kwargs["storage_state"] = str(config.storage_state_file)
            logger.log(f"Carregando storage_state existente: {config.storage_state_file}")
        else:
            logger.log("Nenhum storage_state encontrado; probe segue sem sessao persistida.")

        context = browser.new_context(**context_kwargs)
        page = open_authenticated_course_page(context, config, logger)
        page.set_default_timeout(config.nav_timeout_ms)
        save_storage_state(context, config.storage_state_file)
        logger.log(f"storage_state persistido apos abrir curso: {config.storage_state_file}")

        final_scope = navigate_probe_path(page, logger)
        lessons = collect_video_lessons(page, final_scope, logger)
        payload["lessons"] = lessons
        if not lessons:
            raise RuntimeError("Nenhuma aula de video openMedia4(..., 'V') encontrada no body final.")

        first_lesson = lessons[0]
        payload["first_lesson"] = first_lesson

        network_events: list[dict[str, Any]] = []
        attach_network_capture(page, network_events)
        click_first_video_lesson(page, first_lesson, logger)
        page.wait_for_timeout(2_000)
        wait_for_video_metadata(page, logger)
        page.wait_for_timeout(1_000)

        diagnostics = collect_duration_diagnostics(page)
        payload["duration_diagnostics"] = diagnostics
        payload["network_events"] = network_events[-120:]
        payload["status"] = "ok"

        logger.log(f"[duration] url_atual={diagnostics.get('url', '')}")
        logger.log(
            f"[duration] duration_text_matches="
            f"{json.dumps(diagnostics.get('duration_text_matches', []), ensure_ascii=False)}"
        )
        logger.log(f"[duration] videos={json.dumps(diagnostics.get('videos', []), ensure_ascii=False)[:8000]}")
        logger.log(f"[duration] hidden_inputs={json.dumps(diagnostics.get('hidden_inputs', []), ensure_ascii=False)[:8000]}")
        logger.log(f"[duration] player_elements={json.dumps(diagnostics.get('player_elements', []), ensure_ascii=False)[:12000]}")
        logger.log(f"[network] eventos={json.dumps(payload['network_events'], ensure_ascii=False)[:12000]}")

        output = write_probe_json(probe_dir, payload)
        logger.log(f"Probe concluido com sucesso. JSON={output}")
        return 0
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = str(exc)
        logger.log(f"Probe falhou: {exc}")
        logger.log(traceback.format_exc())
        try:
            output = write_probe_json(probe_dir, payload)
            logger.log(f"JSON parcial gravado em {output}")
        except Exception as write_exc:
            logger.log(f"Falha ao gravar JSON parcial: {write_exc}")
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
