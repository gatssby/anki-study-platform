from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = PROJECT_ROOT / "work/fo_bridge/session/fo_storage_state.json"
TARGET_URL = "https://www.federalonline.com.br/portal/curso-aula/produto-pacote/94d484df6ded73508f3ed1e39dd8d90e/extensivo-ufpr-2026"
OUT_DIR = PROJECT_ROOT / "work/fo_bridge/output/test_downloads"
OUT_DIR.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        storage_state=str(SESSION_FILE),
        accept_downloads=True,
    )
    page = context.new_page()
    page.goto(TARGET_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    # Ajuste inicial: abrir Física > Física I
    page.get_by_text("Física", exact=True).click()
    page.wait_for_timeout(1000)
    page.get_by_text("Física I", exact=True).click()
    page.wait_for_timeout(1500)

    # Clicar no item da lateral "Lista de exercícios"
    page.get_by_text("Lista de exercícios", exact=True).click()
    page.wait_for_timeout(3000)

    # Tenta clicar no botão real de download, se existir na página
    download_btn = page.locator('button[name="btnMaterialDownload"]').first

    try:
        with page.expect_download(timeout=15000) as dl_info:
            if download_btn.count() > 0:
                download_btn.click()
            else:
                # fallback: tenta achar qualquer botão com ícone/label de download
                page.get_by_title("Download").click()

        download = dl_info.value
        target = OUT_DIR / download.suggested_filename
        download.save_as(str(target))
        print("Download salvo em:", target)

    except PlaywrightTimeoutError:
        print("Nenhum download automático foi capturado.")
        print("Provavelmente vamos precisar usar interceptação da resposta PDF.")

    browser.close()
