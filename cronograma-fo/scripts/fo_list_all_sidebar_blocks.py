from playwright.sync_api import sync_playwright
from pathlib import Path

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    blocks = page.evaluate("""
    () => {
        const texts = [...document.querySelectorAll('.card, .accordion, .card-header, .card-title, h5, h6, button, a')]
          .map(el => (el.innerText || '').trim())
          .filter(Boolean)
          .map(t => t.replace(/\\s+/g, ' '))
          .filter(t =>
            t.length > 3 &&
            !t.includes('Vídeo .') &&
            !t.includes('Material') &&
            !t.includes('Resumo') &&
            !t.includes('Slides') &&
            !t.includes('Lista de exercícios') &&
            !t.includes('Gabarito comentado') &&
            !t.startsWith('Aula ')
          );

        return [...new Set(texts)];
    }
    """)

    print("Blocos/textos encontrados:")
    for b in blocks:
        print("-", b)

    browser.close()
