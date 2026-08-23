# apple-music-downloader-py

Porte em Python do [abrinternet/apple-music-downloader](https://github.com/abrinternet/apple-music-downloader) (Go).
Mesma interface de linha de comando, mesmo `config.yaml`, mesmos protocolos externos
(agente Android/Frida para lossless, ffmpeg/MP4Box/mp4decrypt) e o bot do Telegram portado junto.

## Componentes

| Entrada | Equivalente Go | Descrição |
|---|---|---|
| `apple-music-dl` | `main.go` | CLI completa: álbum, playlist, música, station, MV, artista, busca interativa, `--debug`, `--quality-info`, conversão ffmpeg, manifest JSON |
| `apple-music-tgbot` | `cmd/telegram-bot` | Bot sem framework (Bot API bruta), long-polling, fila de downloads, menus interativos, envio incremental dos arquivos |

## Requisitos

- Python 3.11+
- Binários externos: `ffmpeg`, `MP4Box` (GPAC), `mp4decrypt` (Bento4)
- Para lossless (ALAC): o agente `agent.js` via Frida no app Apple Music Android
  (portas 10020/20020) — o protocolo TCP é idêntico ao da versão Go

## Instalação

```bash
cd apple-music-downloader-py
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e .[dev]
```

## Configuração

Copie `config.yaml.example` para `config.yaml` no diretório de execução e preencha
o `media-user-token`. Alternativas: arquivo segredo (`MEDIA_USER_TOKEN_FILE`) ou
variável de ambiente (`MEDIA_USER_TOKEN`). O bot do Telegram usa as mesmas variáveis
de ambiente da versão Go (`TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_ALLOWED_USER_IDS`,
`TELEGRAM_DOWNLOAD_ROOT`, etc.).

## Uso

```bash
apple-music-dl <url-do-album>
apple-music-dl --search album "daft punk"
apple-music-dl --quality-info <url>
apple-music-tgbot
```

## Testes

29 testes unitários/de integração, incluindo round-trip CENC sintético,
CDM auto-consistente (assinatura + derivação de chave com vetor RFC 4493 do CMAC),
protocolo do agente TCP com servidor simulado e conversão TTML→LRC:

```bash
pytest
```

## Docker (Alpine)

Imagem multi-estágio baseada em `python:3.12-alpine` (~150 MB), com `ffmpeg` dos
repositórios Alpine e `mp4decrypt`/`MP4Box` compilados das mesmas revisões
fixadas (com checksum) da imagem Go — o Alpine não tem pacote `gpac`.

Alvos:

- `downloader` — CLI interativa (`docker build --target downloader`)
- `telegram-bot` — bot (`--target telegram-bot`), invoca o CLI via subprocesso

```bash
# CLI pontual (perfil "cli" no compose)
echo "seu-media-user-token" > secrets/media-user-token.txt
docker compose --profile cli run --rm downloader <url>

# Bot em produção (usa o mesmo wrapper Android do deploy Go)
mkdir -p secrets/telegram
printf 'SEU_BOT_TOKEN' > secrets/telegram/bot-token.txt
printf 'SEU_TELEGRAM_ID' > secrets/telegram/allowed-users.txt
docker compose up -d wrapper telegram-bot
```

O `compose.yaml` mantém a topologia do deploy Go: o serviço `wrapper`
(imagem publicada `augustobr/apple-music-wrapper`) expõe as portas
10020/20020/30020 no localhost compartilhado, e os contêineres Python rodam
não-root (`10001`), read-only, sem capabilities.


## Estrutura

- `amdl/` — CLI: `pipeline.py` (fluxos de download), `runv2.py` (ALAC via TCP),
  `runv3/` (Widevine CDM puro em Python + runner), `iso_bmff/` (parser/decrypt MP4),
  `lyrics.py`, `tags.py` (mutagen), `convert.py`, `alacfix.py`
- `amdltgbot/` — bot do Telegram (`client.py`, `bot.py`, `interactive.py`)
- `scripts/extract_device_consts.py` — regenera `amdl/runv3/device_consts.py`
  a partir do `consts.go` do repositório Go
