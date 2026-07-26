from pathlib import Path
from playwright.sync_api import sync_playwright
import base64
import re

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"
OUT_DIR = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/output/test_downloads")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PATH_PARTS = ["Aulas Gerais", "Biologia", "Biologia I"]
MATERIAL_NAME = "Resumo"
OUTPUT_NAME = "Biologia I - Resumo.pdf"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def expand_card_by_title(page, title: str, scope_selector: str | None = None):
    js = """
    ({title, scopeSelector}) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return { ok: false, reason: 'scope_not_found', scopeSelector };

      const cards = [...root.querySelectorAll(':scope > .card, .card')];

      for (const card of cards) {
        const h3 = card.querySelector('.card-header .title h3, .card-header h3');
        const txt = normalize(h3?.innerText || '');
        if (txt !== title) continue;

        const wrapper = card.querySelector('.card-header .header-wrapper');
        const body = card.querySelector(':scope > .card-body, .card-body');
        const target = wrapper?.getAttribute('data-target') || null;

        if (!wrapper) return { ok: false, reason: 'wrapper_not_found', title };

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

def wait_for_body_ready(page, target_selector: str | None, timeout_ms: int = 12000):
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

def click_material_button(page):
    js = """
    () => {
      const btn = document.querySelector('#btnMaterial');
      if (!btn) return {ok:false, reason:'btnMaterial_not_found'};
      btn.click();
      return {ok:true, text:(btn.innerText || btn.title || 'btnMaterial')};
    }
    """
    return page.evaluate(js)

def list_material_rows(page):
    js = """
    () => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const rows = [...document.querySelectorAll('#listDocuments li')];
      return rows.map((row, idx) => {
        const txt = normalize(row.innerText || '');
        const btn = row.querySelector('button[name="btnMaterialViewVideo"], button.btn.btn-light');
        return {
          index: idx,
          text: txt,
          has_button: !!btn
        };
      }).filter(x => x.text);
    }
    """
    return page.evaluate(js)

def click_material_row_by_text(page, material_name: str):
    js = """
    (materialName) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const rows = [...document.querySelectorAll('#listDocuments li')];

      for (const row of rows) {
        const txt = normalize(row.innerText || '');
        if (!txt.includes(materialName)) continue;

        const btn = row.querySelector('button[name="btnMaterialViewVideo"], button.btn.btn-light');
        if (!btn) return { ok:false, reason:'button_not_found', row_text:txt };

        btn.click();
        return { ok:true, row_text:txt };
      }

      return {
        ok:false,
        reason:'material_not_found',
        rows:[...rows].map(r => normalize(r.innerText || '')).filter(Boolean)
      };
    }
    """
    return page.evaluate(js, material_name)

def get_current_blob_info(page):
    return page.evaluate("""
    () => {
        const iframe = document.querySelector('#iFrameDocument');
        return {
            nomeAula: document.querySelector('#nomeAula')?.value || null,
            iframe_src: iframe?.src || null
        };
    }
    """)

def wait_for_expected_material(page, expected_name: str, old_blob: str | None, timeout_ms: int = 15000):
    try:
        page.wait_for_function(
            """
            ({expectedName, oldBlob}) => {
              const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const iframe = document.querySelector('#iFrameDocument');
              const src = iframe?.src || null;
              const title = normalize(document.querySelector('#nomeAula')?.value || '');
              const blobChanged = !!src && src.startsWith('blob:') && src !== oldBlob;
              const titleOk = title === expectedName;
              return blobChanged && titleOk;
            }
            """,
            {"expectedName": expected_name, "oldBlob": old_blob},
            timeout=timeout_ms
        )
        return True
    except Exception:
        return False

def capture_current_blob_pdf(page):
    data = get_current_blob_info(page)
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
    context = browser.new_context(storage_state=str(SESSION_FILE), accept_downloads=True)
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

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

    print("Abrindo painel Material...")
    print(click_material_button(page))
    page.wait_for_timeout(1500)

    rows = list_material_rows(page)
    print("Materiais encontrados:", rows)

    old_info = get_current_blob_info(page)
    old_blob = old_info.get("iframe_src")
    print("Blob anterior:", old_blob)
    print("Título atual:", old_info.get("nomeAula"))

    print(f"Abrindo material: {MATERIAL_NAME}")
    mat = click_material_row_by_text(page, MATERIAL_NAME)
    print("Resultado material:", mat)

    ok = wait_for_expected_material(page, MATERIAL_NAME, old_blob, timeout_ms=15000)
    print("Mudou para material esperado?", ok)

    new_info = get_current_blob_info(page)
    print("Novo estado:", new_info)

    if not ok:
        print("O material correto não abriu. Nada será salvo.")
        browser.close()
        raise SystemExit(1)

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
