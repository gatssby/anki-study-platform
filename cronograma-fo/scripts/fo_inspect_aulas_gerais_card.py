from pathlib import Path
from playwright.sync_api import sync_playwright

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

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

    print("Abrindo Lista de aulas...")
    print(click_smallest_visible_text(page, "Lista de aulas"))
    page.wait_for_timeout(1500)

    print("Clicando Aulas Gerais...")
    print(click_smallest_visible_text(page, "Aulas Gerais"))
    page.wait_for_timeout(1500)

    data = page.evaluate("""
    () => {
      const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();

      const cards = [...document.querySelectorAll('.card')];
      const found = cards.find(card => {
        const h = card.querySelector('.card-header, h3, h4, h5, h6, .accordion-button');
        const txt = normalize(h?.innerText || '');
        return txt === 'Aulas Gerais' || txt.startsWith('Aulas Gerais ');
      });

      if (!found) {
        return { found: false };
      }

      const header = found.querySelector('.card-header, h3, h4, h5, h6, .accordion-button');
      const body = found.querySelector('.card-body, .collapse, .body-list, ul, .list-group');

      const visibleText = (el) => {
        if (!el) return null;
        return normalize(el.innerText || '');
      };

      return {
        found: true,
        card_class: found.className || '',
        header_tag: header?.tagName || null,
        header_class: header?.className || '',
        header_text: visibleText(header),
        header_html: header?.outerHTML || null,
        body_tag: body?.tagName || null,
        body_class: body?.className || '',
        body_text: visibleText(body),
        body_html: body?.outerHTML || null,
        card_html: found.outerHTML || null
      };
    }
    """)

    print("\\n=== INSPEÇÃO DO CARD AULAS GERAIS ===")
    for k, v in data.items():
        print(f"{k}: {v}")
        print()

    browser.close()
