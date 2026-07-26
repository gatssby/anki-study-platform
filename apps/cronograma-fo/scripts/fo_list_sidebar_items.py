from playwright.sync_api import sync_playwright
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = PROJECT_ROOT / "work/fo_bridge/session/fo_storage_state.json"
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    items = page.evaluate("""
    () => {
        const texts = [...document.querySelectorAll('body *')]
          .map(el => (el.innerText || '').trim())
          .filter(Boolean)
          .filter(t =>
            t.includes('Aula 01') ||
            t.includes('Aula 02') ||
            t.includes('Aula 03') ||
            t.includes('Aula 04') ||
            t.includes('Aula 05') ||
            t.includes('Aula 06') ||
            t.includes('Lista de exercícios') ||
            t.includes('Resumo') ||
            t.includes('Slides')
          );

        return [...new Set(texts)].slice(0, 50);
    }
    """)

    print("Itens encontrados:")
    for item in items:
        print("-", item)

    browser.close()
