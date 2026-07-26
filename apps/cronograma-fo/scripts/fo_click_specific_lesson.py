from playwright.sync_api import sync_playwright
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = PROJECT_ROOT / "work/fo_bridge/session/fo_storage_state.json"
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

def read_current(page):
    return page.evaluate("""
    () => ({
        nomeAula: document.querySelector('#nomeAula')?.value || null,
        idVideo: document.querySelector('#idVideo')?.value || null,
        controleDuracao: document.querySelector('#controleDuracao')?.value || null,
        embedSrc: document.querySelector('#embed')?.src || null,
        tipoVideo: document.querySelector('#tipoVideo')?.value || null
    })
    """)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    print("\nANTES:")
    print(read_current(page))

    # garantir que Biologia II esteja expandido
    page.get_by_text("Biologia II", exact=True).click()
    page.wait_for_timeout(1500)

    # clicar numa aula específica pelo texto visível
    page.get_by_text("Aula 03 - Reino Monera", exact=True).click()
    page.wait_for_timeout(4000)

    print("\nDEPOIS DE CLICAR EM 'Aula 03 - Reino Monera':")
    print(read_current(page))

    browser.close()
