from pathlib import Path
from playwright.sync_api import sync_playwright
import re

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def click_smallest_visible_text(page, target: str):
    js = r"""
    (target) => {
      const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' &&
               s.display !== 'none' &&
               r.width > 0 &&
               r.height > 0;
      };

      const tags = ['button','a','li','span','h3','h4','h5','h6','div'];
      let candidates = [];

      for (const tag of tags) {
        for (const el of document.querySelectorAll(tag)) {
          if (!isVisible(el)) continue;
          const txt = normalize(el.innerText || '');
          if (!txt) continue;
          if (txt === target || txt.startsWith(target + ' ')) {
            const rect = el.getBoundingClientRect();
            candidates.push({
              el,
              tag: el.tagName,
              text: txt,
              area: rect.width * rect.height
            });
          }
        }
      }

      candidates.sort((a, b) => a.area - b.area);

      for (const c of candidates) {
        try {
          c.el.click();
          return {
            ok: true,
            tag: c.tag,
            text: c.text,
            area: c.area,
            candidates: candidates.length
          };
        } catch (e) {}
      }

      return { ok: false, candidates: candidates.length };
    }
    """
    return page.evaluate(js, target)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    print("URL atual:", page.url)

    print("Abrindo Aulas Gerais...")
    res = click_smallest_visible_text(page, "Aulas Gerais")
    print("Resultado:", res)
    page.wait_for_timeout(2000)

    data = page.evaluate("""
    () => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' &&
               s.display !== 'none' &&
               r.width > 0 &&
               r.height > 0;
      };

      const tags = ['h3','h4','h5','h6','li','button','a','span','div'];
      let rows = [];

      for (const tag of tags) {
        for (const el of document.querySelectorAll(tag)) {
          if (!isVisible(el)) continue;
          const txt = normalize(el.innerText || '');
          if (!txt) continue;
          if (txt.length > 120) continue;

          rows.push({
            tag: el.tagName,
            text: txt
          });
        }
      }

      // remove duplicados preservando ordem
      const seen = new Set();
      const out = [];
      for (const row of rows) {
        const key = row.tag + "||" + row.text;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(row);
      }
      return out.slice(0, 300);
    }
    """)

    print("\\nElementos visíveis após abrir Aulas Gerais:")
    for row in data:
        print(f"{row['tag']}: {row['text']}")

    browser.close()
