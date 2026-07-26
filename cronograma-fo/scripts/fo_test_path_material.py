from pathlib import Path
from playwright.sync_api import sync_playwright
import base64
import re

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"
OUT_DIR = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/output/test_downloads")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PATH_PARTS = ["Aulas Gerais", "Física", "Física I"]
MATERIAL_NAME = "Lista de exercícios"
OUTPUT_NAME = "Física I - Lista de exercícios.pdf"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def expand_card_by_title(page, title: str, scope_selector: str | None = None):
    js = """
    ({title, scopeSelector}) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();

      const root = scopeSelector
        ? document.querySelector(scopeSelector)
        : document;

      if (!root) {
        return { ok: false, reason: 'scope_not_found', scopeSelector };
      }

      const cards = [...root.querySelectorAll(':scope > .card, .card')];

      for (const card of cards) {
        const h3 = card.querySelector('.card-header .title h3, .card-header h3');
        const txt = normalize(h3?.innerText || '');
        if (txt !== title) continue;

        const wrapper = card.querySelector('.card-header .header-wrapper');
        const body = card.querySelector(':scope > .card-body, .card-body');
        const target = wrapper?.getAttribute('data-target') || null;

        if (!wrapper) {
          return { ok: false, reason: 'wrapper_not_found', title };
        }

        wrapper.click();

        return {
          ok: true,
          title,
          target,
          body_id: body?.id || null,
          body_class: body?.className || null
        };
      }

      return { ok: false, reason: 'title_not_found', title };
    }
    """
    return page.evaluate(js, {"title": title, "scopeSelector": scope_selector})

def wait_for_body_ready(page, target_selector: str | None, timeout_ms: int = 10000):
    if not target_selector:
        page.wait_for_timeout(1500)
        return

    try:
        page.wait_for_function(
            """
            (sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;

              const cls = el.className || '';
              const txt = (el.innerText || '').trim();

              return cls.includes('show') || txt !== 'Aguarde carregando...' || txt.length > 30;
            }
            """,
            target_selector,
            timeout=timeout_ms
        )
    except Exception:
        page.wait_for_timeout(2000)

def get_nested_scope_from_target(target: str | None) -> str | None:
    if not target:
        return None
    if target.startswith("#"):
        return target
    return f"#{target}"

def click_material_row(page, target_material: str):
    js = """
    (targetMaterial) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' &&
               s.display !== 'none' &&
               r.width > 0 &&
               r.height > 0;
      };

      const rows = [...document.querySelectorAll('#listDocuments li')].filter(isVisible);

      for (const row of rows) {
        const txt = normalize(row.innerText || '');
        if (txt.includes(targetMaterial)) {
          const btn = row.querySelector('button[name="btnMaterialViewVideo"], button.btn.btn-light');
          if (btn) {
            btn.click();
            return { ok: true, row_text: txt };
          }
        }
      }

      return { ok: false, rows: rows.map(r => normalize(r.innerText || '')) };
    }
    """
    return page.evaluate(js, target_material)

def capture_current_blob_pdf(page):
    data = page.evaluate("""
    () => {
        const iframe = document.querySelector('#iFrameDocument');
        const objectEl = document.querySelector('object');
        const embedEl = document.querySelector('embed');
        return {
            nomeAula: document.querySelector('#nomeAula')?.value || null,
            iframe_src: iframe?.src || null,
            object_data: objectEl?.data || null,
            embed_src: embedEl?.src || null
        };
    }
    """)

    blob_url = data.get("iframe_src")
    if not blob_url or not blob_url.startswith("blob:"):
        return None, data

    b64 = page.evaluate("""
    async (blobUrl) => {
        const resp = await fetch(blobUrl);
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let binary = "";
        const chunkSize = 0x8000;
        for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
        }
        return btoa(binary);
    }
    """, blob_url)

    return base64.b64decode(b64), data

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        storage_state=str(SESSION_FILE),
        accept_downloads=True,
    )
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    print("URL atual:", page.url)

    current_scope = None

    for part in PATH_PARTS:
        print(f"Abrindo: {part} | scope={current_scope}")
        res = expand_card_by_title(page, part, current_scope)
        print("Resultado:", res)

        if not res.get("ok"):
            browser.close()
            raise SystemExit(1)

        target = res.get("target")
        target_selector = target if (target and target.startswith("#")) else (f"#{target}" if target else None)
        wait_for_body_ready(page, target_selector, timeout_ms=12000)

        current_scope = get_nested_scope_from_target(target)

        page.wait_for_timeout(1200)

    print(f"Abrindo material: {MATERIAL_NAME}")
    mat = click_material_row(page, MATERIAL_NAME)
    print("Resultado material:", mat)
    page.wait_for_timeout(5000)

    pdf_bytes, data = capture_current_blob_pdf(page)
    print("Dados capturados:", data)

    if not pdf_bytes:
        print("Não consegui capturar blob PDF.")
        browser.close()
        raise SystemExit(1)

    out_path = OUT_DIR / OUTPUT_NAME
    out_path.write_bytes(pdf_bytes)

    print("Tamanho salvo (bytes):", len(pdf_bytes))
    print("Arquivo salvo em:", out_path)

    browser.close()
