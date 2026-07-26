# Assets visuais locais

## Tabler Core

- Versão: `1.4.0`
- Origem: `@tabler/core` no npm (`dist/css/tabler.min.css`)
- Arquivo: `tabler.min.css`
- Tamanho: `536141` bytes
- SHA-256: `7ef750bd10546a695d0b12767ad8048bd8f3ec5de7daefb1067f9d0daa3d1c9a`
- Licença: MIT, preservada em `LICENSE`

## Tabler Icons

- Versão: `3.44.0`
- Origem: repositório `tabler/tabler-icons`, diretório `icons/outline`
- Licença: MIT, preservada em `../tabler-icons/LICENSE`
- Seleção: somente os SVGs usados pelos templates desta etapa

| Arquivo | Bytes | SHA-256 |
| --- | ---: | --- |
| `alert-triangle.svg` | 563 | `fc82f02dc9702293cb8609a8aed3242c0fe5f5b3337d79d341aa9343b4526ad4` |
| `book-2.svg` | 482 | `1f03d3e6df24ba50d7d9007d344430fe96fd80a3e4e5da92ecdd4dcf024f7a3a` |
| `calendar-cog.svg` | 780 | `15877fad8f9e2ef00aa4c03b8815fac7151f362100ba9df8e62b61459a8ab0f0` |
| `calendar.svg` | 568 | `97faa5ae97a558ba4a427b3ec576b11670cc0d1dbcc128c44507fa54e831451f` |
| `cards.svg` | 666 | `ae6992c1e582f568116d3f3c61ecdac9726fc6b7ab0ac1bc6c8e824a4ddf80e2` |
| `check.svg` | 395 | `fe359b27c74ed0f4f72bfabbe5ca969a8bb13a5f39648bae63f9e798034ebed3` |
| `chevron-down.svg` | 377 | `06bd20b8cbe565b97046c5d0ec917f11ed6b29c0e4885a82ae057325c972bf9a` |
| `circle-check.svg` | 429 | `60bb8534e78f5db10f1425170cfe03245ae369a1a3e788414319528f6d06cf76` |
| `clock.svg` | 431 | `bb582eb2c9d6709d285623bea586e1f532872a9db3d2391308360636e93d199d` |
| `database.svg` | 497 | `cf6f390ac33cfa83f054547137ce20e1702f9cc69af10db314ee1dc879f86df7` |
| `download.svg` | 450 | `4c3498b4dc4e7f007db234f293b396ae1b23f18646e1d36f8bc4e08a31bf97b1` |
| `layout-dashboard.svg` | 720 | `fa525ddc80d1df3411fe44685eeb3bd03d1b285581049931efe648450486eaf7` |
| `list-check.svg` | 553 | `ae5a54fef119adc6b63e6a5aeb374535ee7c4a8f266dfa86cef6e769496a2996` |

## Arquitetura visual final

O Tabler é carregado primeiro como base local. Os estilos próprios são divididos por responsabilidade:

- `tokens.css`: cores, espaçamento, raios, sombras e duração de transições;
- `app.css`: shell, header, menu mobile, botões, formulários e componentes compartilhados;
- `dashboard.css`: cronograma FO/UN e exercícios;
- `database.css`: filtros, tabela desktop e cards mobile da base;
- `review.css`: listagem, formulário, preview e sessão de revisão;
- `reprogramming.css`: calendário, resumo e modal;
- `responsive.css`: ajustes compartilhados de breakpoints.

`style.css`, as fontes remotas e o shell visual antigo foram removidos após a migração de todos os templates ativos. MathJax permanece inalterado e separado desta fundação visual.
