from playwright.sync_api import sync_playwright
from pathlib import Path

from fo_session_security import save_storage_state

SESSION_FILE = Path("/Users/gatsby/Workspace/Cronograma FO/work/fo_bridge/session/fo_storage_state.json")
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto(TARGET_URL, wait_until="domcontentloaded")

    print("\nFaça login manualmente no Federal Online.")
    print("Quando estiver dentro da página do curso já logado, volte ao terminal e pressione ENTER.\n")
    input()

    save_storage_state(context, SESSION_FILE)
    print(f"Sessão salva em: {SESSION_FILE}")

    browser.close()
