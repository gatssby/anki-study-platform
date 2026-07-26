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

    data = page.evaluate("""
    () => ({
        nomeAula: document.querySelector('#nomeAula')?.value || null,
        idVideo: document.querySelector('#idVideo')?.value || null,
        controleDuracao: document.querySelector('#controleDuracao')?.value || null,
        embedSrc: document.querySelector('#embed')?.src || null,
        tipoVideo: document.querySelector('#tipoVideo')?.value || null
    })
    """)

    print(data)

    browser.close()
