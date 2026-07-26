# Dependências por runtime

## API Anki

Python 3.10 na VPS. O servidor é majoritariamente standard library; `pypdf`
6.10.2 é usado por rotas opcionais de PDF.

## Cronograma web

Python 3.12 no container. As versões estão fixadas em
`apps/cronograma-fo/requirements.txt`.

## Jobs FO

O host da VPS possui Playwright 1.58.0, certifi 2020.6.20, PyMuPDF 1.28.0 e
pypdf 6.10.2. FFmpeg 4.4.2 e rclone 1.73.1 são dependências de sistema.
Playwright requer ainda o navegador Chromium compatível.

## Addon

O addon depende do Python, PyQt e módulos fornecidos pelo Anki. Ele não requer
nem admite um pacote Python global externo; helpers compartilhados necessários
devem ser vendorizados ou incluídos no bundle.

## Desenvolvimento

`requirements-dev.txt` agrega ferramentas de teste, lint, segurança e validação
OpenAPI. Ambientes devem ser isolados por venv ou container.
