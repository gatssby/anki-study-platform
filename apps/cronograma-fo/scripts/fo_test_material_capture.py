from pathlib import Path
from playwright.sync_api import sync_playwright
import base64

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = PROJECT_ROOT / "work/fo_bridge/session/fo_storage_state.json"
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"
OUT_DIR = PROJECT_ROOT / "work/fo_bridge/output/test_downloads"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SUBJECT_NAME = "Física I"
MATERIAL_NAME = "Lista de exercícios"

def save_name(subject: str, material: str) -> str:
    return f"{subject} - {material}.pdf"

def click_visible_text(page, text: str):
    js = """
    (text) => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' &&
               s.display !== 'none' &&
               r.width > 0 &&
               r.height > 0;
      };

      const all = [...document.querySelectorAll('body *')];
      const candidates = all.filter(el => normalize(el.innerText) === text && isVisible(el));

      for (const el of candidates) {
        const clickable = el.closest('button, a, li, div, span');
        if (clickable && isVisible(clickable)) {
          clickable.click();
          return {
            ok: true,
            tag: clickable.tagName,
            text: normalize(clickable.innerText || '')
          };
        }
      }

      return { ok: false, found: candidates.length };
    }
    """
    return page.evaluate(js, text)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        storage_state=str(SESSION_FILE),
        accept_downloads=True,
    )
    page = context.new_page()

    pdf_like_urls = []

    def handle_response(resp):
        try:
            url = resp.url
            ctype = (resp.headers.get("content-type") or "").lower()
            if (
                "/portal/documento-online/" in url.lower()
                or "application/pdf" in ctype
                or url.lower().startswith("blob:")
            ):
                pdf_like_urls.append((url, ctype, resp.status))
                print("PDF-like response:", resp.status, ctype, url)
        except Exception as e:
            print("Erro ao processar response:", e)

    page.on("response", handle_response)

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    print("Abrindo Física...")
    print(click_visible_text(page, "Física"))
    page.wait_for_timeout(1200)

    print(f"Abrindo {MATERIAL_NAME}...")
    print(click_visible_text(page, MATERIAL_NAME))
    page.wait_for_timeout(6000)

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

    print("Dados capturados:", data)
    print("PDF-like URLs vistos até agora:", pdf_like_urls[-10:])

    blob_url = data.get("iframe_src")
    if not blob_url or not blob_url.startswith("blob:"):
        print("Não encontrei blob URL no iframe.")
        browser.close()
        raise SystemExit(1)

    print("Blob URL usada:", blob_url)

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

    pdf_bytes = base64.b64decode(b64)
    out_path = OUT_DIR / save_name(SUBJECT_NAME, MATERIAL_NAME)
    out_path.write_bytes(pdf_bytes)

    print("Tamanho salvo (bytes):", len(pdf_bytes))
    print("Arquivo salvo em:", out_path)

    browser.close()
