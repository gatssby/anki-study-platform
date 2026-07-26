from pathlib import Path
from playwright.sync_api import sync_playwright
import json
import re
from datetime import datetime

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
COURSE_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

OUT_DIR = Path("/Users/gatsby/Workspace/Anki GPT/files/misc/fo_probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PATH_PARTS = ["Aulas Gerais", "Biologia", "Biologia I"]
MATERIAL_NAME = "Resumo"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def stamp(name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}_{name}"

def save_text(path: Path, text: str):
    path.write_text(text, encoding="utf-8")

def dump_page(page, label: str):
    html_path = OUT_DIR / f"{label}.html"
    png_path = OUT_DIR / f"{label}.png"
    state_path = OUT_DIR / f"{label}.json"

    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=True)

    state = page.evaluate("""
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const vis = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
      };

      const cards = [...document.querySelectorAll('.card')].map((card, idx) => {
        const h3 = card.querySelector('.card-header .title h3, .card-header h3');
        const wrapper = card.querySelector('.card-header .header-wrapper');
        const body = card.querySelector(':scope > .card-body, .card-body');
        return {
          idx,
          title: norm(h3?.innerText || ''),
          wrapperClass: wrapper?.className || null,
          dataTarget: wrapper?.getAttribute('data-target') || null,
          bodyId: body?.id || null,
          bodyClass: body?.className || null,
          visible: vis(card),
          text: norm(card.innerText || '').slice(0, 400)
        };
      });

      const materials = [...document.querySelectorAll('#listDocuments li')].map((li, idx) => ({
        idx,
        text: norm(li.innerText || ''),
      })).filter(x => x.text);

      return {
        url: location.href,
        nomeAula: document.querySelector('#nomeAula')?.value || null,
        iframeSrc: document.querySelector('#iFrameDocument')?.src || null,
        btnMaterialExists: !!document.querySelector('#btnMaterial'),
        cards,
        materials
      };
    }
    """)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dump] {label}")
    print(f"  html:  {html_path}")
    print(f"  png:   {png_path}")
    print(f"  state: {state_path}")

def neutralize_intro(page):
    page.evaluate("""
    () => {
      const sels = [
        '.introjs-overlay',
        '.introjs-helperLayer',
        '.introjs-tooltipReferenceLayer',
        '.introjs-tooltip',
        '.introjs-showElement'
      ];
      for (const sel of sels) {
        document.querySelectorAll(sel).forEach(el => el.remove());
      }
      for (const txt of ['Fechar','Pular','Skip','Close','×']) {
        for (const el of [...document.querySelectorAll('button, a, span, div')]) {
          const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
          if (t === txt) {
            try { el.click(); } catch {}
          }
        }
      }
    }
    """)

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

        try {
          wrapper.click();
          return {
            ok: true,
            title,
            target,
            bodyId: body?.id || null,
            bodyClass: body?.className || null,
            method: 'native_click'
          };
        } catch (e) {
          try {
            wrapper.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
            return {
              ok: true,
              title,
              target,
              bodyId: body?.id || null,
              bodyClass: body?.className || null,
              method: 'dispatch_click'
            };
          } catch (e2) {
            return { ok: false, reason: 'click_failed', title, error: String(e2) };
          }
        }
      }

      return { ok: false, reason: 'title_not_found', title };
    }
    """
    return page.evaluate(js, {"title": title, "scopeSelector": scope_selector})

def wait_for_body_ready(page, target_selector: str | None, timeout_ms: int = 12000):
    if not target_selector:
        page.wait_for_timeout(1500)
        return False
    try:
        page.wait_for_function(
            """
            (sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              const cls = el.className || '';
              const txt = (el.innerText || '').trim();
              return cls.includes('show') || cls.includes('collapse') || txt.length > 0;
            }
            """,
            target_selector,
            timeout=timeout_ms
        )
        return True
    except Exception:
        return False

def get_nested_scope_from_target(target: str | None) -> str | None:
    if not target:
        return None
    return target if target.startswith("#") else f"#{target}"

def click_btn_material(page):
    return page.evaluate("""
    () => {
      const btn = document.querySelector('#btnMaterial');
      if (!btn) return {ok:false, reason:'btnMaterial_not_found'};
      try {
        btn.click();
        return {ok:true, text:(btn.innerText || btn.title || 'btnMaterial')};
      } catch (e) {
        return {ok:false, reason:'btnMaterial_click_failed', error:String(e)};
      }
    }
    """)

def click_material_row(page, target_material: str):
    return page.evaluate("""
    (targetMaterial) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const rows = [...document.querySelectorAll('#listDocuments li')];

      for (const row of rows) {
        const txt = norm(row.innerText || '');
        if (!txt.includes(targetMaterial)) continue;
        const btn = row.querySelector('button[name="btnMaterialViewVideo"], button.btn.btn-light');
        if (!btn) return {ok:false, reason:'button_not_found', rowText:txt};
        try {
          btn.click();
          return {ok:true, rowText:txt, method:'native_click'};
        } catch (e) {
          try {
            btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
            return {ok:true, rowText:txt, method:'dispatch_click'};
          } catch (e2) {
            return {ok:false, reason:'click_failed', rowText:txt, error:String(e2)};
          }
        }
      }

      return {
        ok:false,
        reason:'material_not_found',
        rows:[...rows].map(r => norm(r.innerText || '')).filter(Boolean)
      };
    }
    """, target_material)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()

    netlog = []

    def on_request(req):
        url = req.url
        if any(x in url.lower() for x in ["documento-online", "curso-aula", "media", "material"]):
            netlog.append({"type":"request","method":req.method,"url":url})

    def on_response(resp):
        url = resp.url
        if any(x in url.lower() for x in ["documento-online", "curso-aula", "media", "material"]):
            netlog.append({"type":"response","status":resp.status,"url":url,"content_type":resp.headers.get("content-type")})

    page.on("request", on_request)
    page.on("response", on_response)

    page.goto(COURSE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)
    neutralize_intro(page)
    dump_page(page, stamp("00_loaded"))

    current_scope = None
    for i, part in enumerate(PATH_PARTS, start=1):
        neutralize_intro(page)
        print(f"[nav] abrindo {part} | scope={current_scope}")
        res = expand_card_by_title(page, part, current_scope)
        print(json.dumps(res, ensure_ascii=False))
        target = res.get("target")
        target_selector = target if (target and target.startswith("#")) else (f"#{target}" if target else None)
        ready = wait_for_body_ready(page, target_selector, timeout_ms=12000)
        print(f"[nav] ready={ready} target_selector={target_selector}")
        current_scope = get_nested_scope_from_target(target)
        page.wait_for_timeout(1500)
        dump_page(page, stamp(f"{i:02d}_{part.replace(' ', '_')}"))

    neutralize_intro(page)
    print("[material] abrindo painel material")
    print(json.dumps(click_btn_material(page), ensure_ascii=False))
    page.wait_for_timeout(1500)
    dump_page(page, stamp("10_material_panel"))

    print(f"[material] abrindo {MATERIAL_NAME}")
    res = click_material_row(page, MATERIAL_NAME)
    print(json.dumps(res, ensure_ascii=False))
    page.wait_for_timeout(6000)
    dump_page(page, stamp("11_material_clicked"))

    (OUT_DIR / "network_log.json").write_text(json.dumps(netlog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] network_log: {OUT_DIR / 'network_log.json'}")

    browser.close()
