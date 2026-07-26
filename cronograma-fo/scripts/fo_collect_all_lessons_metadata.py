import argparse
import csv
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_DIR = Path(__file__).resolve().parents[1]
SESSION_FILE = BASE_DIR / "work/fo_bridge/session/fo_storage_state.json"
OUTPUT_FILE = BASE_DIR / "work/fo_bridge/output/fo_all_lessons_metadata.csv"
TARGET_URL = (
    "https://www.federalonline.com.br/portal/curso-aula/"
    "produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"
)

CSV_COLUMNS = [
    "subject_group",
    "lesson_title",
    "id_video",
    "duration_seconds",
    "embed_url",
    "tipo_video",
    "order_in_subject",
    "source_url",
    "clicked_text",
]

IGNORE_KEYWORDS = (
    "lista de exercícios",
    "resumo",
    "slides",
    "gabarito",
)


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_label(value: str | None) -> str:
    text = normalize_space(value)
    return re.sub(r"\s+\d+%$", "", text).strip()


def slug_key(value: str | None) -> str:
    return normalize_space(value).casefold()


def normalize_sidebar_title(value: str | None) -> str:
    text = normalize_space(value)
    if not text:
        return ""
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


def read_current_video(page: Page) -> dict[str, str | None]:
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


def ensure_output_header() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT_FILE.exists():
        return
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def load_existing_entries() -> tuple[set[tuple[str, str]], set[tuple[str, str]], int]:
    if not OUTPUT_FILE.exists():
        return set(), set(), 0

    by_video_id: set[tuple[str, str]] = set()
    by_title: set[tuple[str, str]] = set()
    row_count = 0

    with OUTPUT_FILE.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            subject_group = row.get("subject_group", "").strip()
            id_video = row.get("id_video", "").strip()
            lesson_title = row.get("lesson_title", "").strip()
            if subject_group and id_video:
                by_video_id.add((subject_group, id_video))
            if subject_group and lesson_title:
                by_title.add((subject_group, slug_key(lesson_title)))

    return by_video_id, by_title, row_count


def append_row(row: dict[str, object]) -> None:
    with OUTPUT_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writerow(row)


def wait_until(predicate, timeout_seconds: float, error_message: str) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError(error_message)


def wait_for_course_ready(page: Page) -> None:
    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.locator("#topicsAccordion").wait_for(timeout=20_000)
    page.locator("#idVideo").wait_for(state="attached", timeout=20_000)

    if "/portal/curso-aula/" not in page.url:
        raise RuntimeError(
            "A sessão não abriu diretamente a página do curso. "
            "Revalide o arquivo de sessão em work/fo_bridge/session/fo_storage_state.json."
        )


def get_card_title(card: Locator) -> str:
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


def body_is_loaded(body: Locator) -> bool:
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


def resolve_descendant_path(container: Locator, path: list[int]) -> Locator:
    node = container
    for index in path:
        node = node.locator(":scope > *").nth(index)
    return node


def list_logical_descendant_paths(
    container: Locator,
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


def expand_card(card: Locator, path_label: str) -> Locator:
    header = card.locator(":scope > .card-header")
    body = card.locator(":scope > .card-body")
    body_class = body.get_attribute("class") or ""

    if "show" not in body_class:
        header.scroll_into_view_if_needed()
        header.click()

    wait_until(
        lambda: "show" in (body.get_attribute("class") or ""),
        timeout_seconds=15,
        error_message=f"Timeout ao expandir o bloco '{path_label}'.",
    )
    wait_until(
        lambda: body_is_loaded(body),
        timeout_seconds=20,
        error_message=f"Timeout aguardando conteúdo do bloco '{path_label}'.",
    )

    return body


def extract_item_info(item: Locator) -> dict[str, str | None]:
    return item.evaluate(
        """
        (el) => {
            const cleanTitle = (text) => (text || '')
                .replace(/\\s+/g, ' ')
                .replace(/\\s+Vídeo\\s*\\..*$/i, '')
                .replace(/\\s+Material.*$/i, '')
                .trim();

            const rawText = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            const clickableText = rawText.replace(/^Marcar como visto\\s*/i, '').trim();
            const videoNode = el.querySelector('[data-video]');
            const dataVideo = videoNode?.getAttribute('data-video') || null;
            const onclick = el.getAttribute('onclick') || '';

            const spanTexts = [...el.querySelectorAll('span')]
                .map((node) => cleanTitle(node.innerText || ''))
                .filter(Boolean);

            const explicitTitle = spanTexts.find((text) => /^Aula\\s+/i.test(text)) || null;
            const fallbackTitle = cleanTitle(clickableText);

            let mediaType = null;
            const mediaTypeMatch = onclick.match(/'([A-Z])'\\)\\s*$/);
            if (mediaTypeMatch) {
                mediaType = mediaTypeMatch[1];
            } else if (dataVideo) {
                mediaType = 'V';
            }

            return {
                rawText,
                clickedText: clickableText,
                title: explicitTitle || fallbackTitle || null,
                dataVideo,
                mediaType,
            };
        }
        """
    )


def is_ignored_item(item_info: dict[str, str | None]) -> bool:
    haystack = " ".join(
        normalize_space(value)
        for value in (
            item_info.get("title"),
            item_info.get("clickedText"),
            item_info.get("rawText"),
        )
        if value
    ).casefold()
    return any(keyword in haystack for keyword in IGNORE_KEYWORDS)


def wait_for_selected_video(
    page: Page,
    previous_state: dict[str, str | None],
    expected_id: str | None,
    expected_title: str | None,
) -> dict[str, str | None]:
    previous_id = normalize_space(previous_state.get("idVideo"))
    previous_title = normalize_space(previous_state.get("nomeAula"))
    expected_title_key = slug_key(expected_title)
    stable_match_hits = 0

    deadline = time.time() + 20
    while time.time() < deadline:
        current = read_current_video(page)
        current_id = normalize_space(current.get("idVideo"))
        current_title = normalize_space(current.get("nomeAula"))
        current_title_key = slug_key(current_title)

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
    if expected_title_key and slug_key(current_title) == expected_title_key:
        return current
    if expected_id and current_id == expected_id:
        return current

    raise RuntimeError(
        "Timeout aguardando a aula carregar após o clique. "
        f"Anterior: id={previous_id or '-'} título={previous_title or '-'} | "
        f"Esperado: id_hint={expected_id or '-'} título={expected_title or '-'}."
    )


def collect_direct_videos(
    page: Page,
    body: Locator,
    subject_group: str,
    collected_by_video: set[tuple[str, str]],
    collected_by_title: set[tuple[str, str]],
    limit: int | None,
    stats: dict[str, int],
) -> bool:
    item_paths = list_logical_descendant_paths(
        container=body,
        target_selector=".body-list li",
        blocked_ancestor_selector=".card",
    )
    item_count = len(item_paths)

    if item_count == 0:
        return False

    video_order = 0
    for item_path in item_paths:
        item = resolve_descendant_path(body, item_path)
        item_info = extract_item_info(item)

        if item_info.get("mediaType") != "V":
            continue
        if is_ignored_item(item_info):
            continue

        video_order += 1

        lesson_title_hint = normalize_sidebar_title(item_info.get("title")) or normalize_sidebar_title(
            item_info.get("clickedText")
        )
        video_id_hint = normalize_space(item_info.get("dataVideo"))
        title_key = (subject_group, slug_key(lesson_title_hint))
        video_key = (subject_group, video_id_hint) if video_id_hint else None

        if video_key and video_key in collected_by_video:
            stats["skipped"] += 1
            print(f"[skip] {subject_group} :: {lesson_title_hint or video_id_hint}")
            continue
        if lesson_title_hint and title_key in collected_by_title:
            stats["skipped"] += 1
            print(f"[skip] {subject_group} :: {lesson_title_hint}")
            continue

        previous_state = read_current_video(page)
        item.scroll_into_view_if_needed()
        item.click()
        try:
            current = wait_for_selected_video(
                page,
                previous_state=previous_state,
                expected_id=video_id_hint or None,
                expected_title=lesson_title_hint or None,
            )
        except RuntimeError as exc:
            stats["failed"] += 1
            print(
                f"[warn] {subject_group} :: {lesson_title_hint or video_id_hint} "
                f"não confirmou troca de aula; item pulado. {exc}"
            )
            continue

        row = {
            "subject_group": subject_group,
            "lesson_title": normalize_space(current.get("nomeAula")) or lesson_title_hint,
            "id_video": normalize_space(current.get("idVideo")) or video_id_hint,
            "duration_seconds": parse_duration_seconds(current.get("controleDuracao")),
            "embed_url": normalize_space(current.get("embedSrc")),
            "tipo_video": normalize_space(current.get("tipoVideo")),
            "order_in_subject": video_order,
            "source_url": page.url,
            "clicked_text": normalize_space(item_info.get("clickedText")),
        }

        append_row(row)

        final_video_id = str(row["id_video"] or "").strip()
        final_title = str(row["lesson_title"] or "").strip()
        if final_video_id:
            collected_by_video.add((subject_group, final_video_id))
        if final_title:
            collected_by_title.add((subject_group, slug_key(final_title)))

        stats["written"] += 1
        print(
            f"[write] {subject_group} :: {final_title or final_video_id} "
            f"(ordem {video_order})"
        )

        if limit is not None and stats["written"] >= limit:
            return True

    return False


def walk_cards(
    page: Page,
    container: Locator,
    breadcrumbs: list[str],
    collected_by_video: set[tuple[str, str]],
    collected_by_title: set[tuple[str, str]],
    limit: int | None,
    stats: dict[str, int],
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
        path_label = " > ".join(current_path) if current_path else "(sem título)"

        body = expand_card(card, path_label)
        should_stop = collect_direct_videos(
            page=page,
            body=body,
            subject_group=path_label,
            collected_by_video=collected_by_video,
            collected_by_title=collected_by_title,
            limit=limit,
            stats=stats,
        )
        if should_stop:
            return True

        should_stop = walk_cards(
            page=page,
            container=body,
            breadcrumbs=current_path,
            collected_by_video=collected_by_video,
            collected_by_title=collected_by_title,
            limit=limit,
            stats=stats,
        )
        if should_stop:
            return True

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta metadados de todas as videoaulas do curso do Federal Online."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Executa o navegador em headless.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita a quantidade de novas aulas gravadas nesta execução.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not SESSION_FILE.exists():
        print(f"Sessão não encontrada em {SESSION_FILE}", file=sys.stderr)
        return 1

    ensure_output_header()
    collected_by_video, collected_by_title, existing_rows = load_existing_entries()

    print(f"CSV: {OUTPUT_FILE}")
    print(f"Linhas já existentes: {existing_rows}")
    if args.limit is not None:
        print(f"Limite de novas gravações nesta execução: {args.limit}")

    stats = {"written": 0, "skipped": 0, "failed": 0}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()

        try:
            wait_for_course_ready(page)
            root = page.locator("#topicsAccordion")
            walk_cards(
                page=page,
                container=root,
                breadcrumbs=[],
                collected_by_video=collected_by_video,
                collected_by_title=collected_by_title,
                limit=args.limit,
                stats=stats,
            )
        except (PlaywrightTimeoutError, RuntimeError) as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return_code = 1
        else:
            return_code = 0
        finally:
            context.close()
            browser.close()

    print(
        "Resumo da execução: "
        f"{stats['written']} novas aulas gravadas, "
        f"{stats['skipped']} aulas já existentes puladas, "
        f"{stats['failed']} itens sem troca confirmada pulados."
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
