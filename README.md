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


## Estabilidade e limites do bot

`TELEGRAM_JOB_TIMEOUT` limita cada pedido (download e envio) em segundos;
o padrão é `21600` (6 horas). `/cancel` interrompe inclusive uma conexão de
upload sem resposta. Falhas de um pedido ou de uma resposta do Telegram não
encerram o atendimento nem o trabalhador da fila. Arquivos só são removidos
depois da confirmação de envio.

O processamento ALAC lê os metadados e um pacote por vez; AAC/Widevine é
decodificado por fragmento, e segmentos de vídeo são gravados diretamente
em disco, em ordem. O espaço temporário continua necessário em
`TELEGRAM_DOWNLOADER_TEMP_DIR` (por padrão `/downloads/.tmp`).

No `deploy/telegram-server/update_bots.sh` do repositório Go, use
`APPLE_MUSIC_BOT_ENGINE=python` para selecionar `apple-music-telegram-bot-py`.
`APPLE_MUSIC_BOT_SOURCE` permite fixar uma tag ou digest específico.

## Estrutura dos pacotes

- `amdl/` — CLI: `pipeline.py` (fluxos de download), `runv2.py` (ALAC via TCP),
  `runv3/` (Widevine CDM puro em Python + runner), `iso_bmff/` (parser/decrypt MP4),
  `lyrics.py`, `tags.py` (mutagen), `convert.py`, `alacfix.py`
- `amdltgbot/` — bot do Telegram (`client.py`, `bot.py`, `interactive.py`)
- `scripts/extract_device_consts.py` — regenera `amdl/runv3/device_consts.py`
  a partir do `consts.go` do repositório Go
# Recuperacao do bot

Pedidos aceitos e confirmacoes de envio sao persistidos em `/downloads/.jobs`.
Reiniciar o container preserva a fila, desde que o volume seja mantido. Pedidos
incompletos voltam a ser tentados apos 60 segundos, permitindo que outros pedidos
avancem. `/cancel` cancela tambem pedidos do usuario aguardando recuperacao.

`TELEGRAM_IDLE_TIMEOUT=600` limita o tempo sem novos arquivos ou envios;
`TELEGRAM_JOB_TIMEOUT=21600` limita cada tentativa. Arquivos sao excluidos somente
apos confirmar e persistir o envio. O downloader consulta o registro do pedido
para pular arquivos ja entregues, sem precisar conserva-los no disco.

Em containers separados, configure `WRAPPER_ACCOUNT_URL`,
`WRAPPER_DECRYPT_ADDRESS` e `WRAPPER_M3U8_ADDRESS` com o DNS do wrapper.
O monitor externo e o Compose de producao estao no repositorio Go, em
`deploy/telegram-server/update_bots.sh`. Eles atendem aos dois motores.

Credenciais revogadas e conteudo indisponivel podem impedir a conclusao ate que
a causa seja resolvida. Uma resposta perdida depois de o Telegram aceitar um
arquivo pode causar duplicacao; `sendDocument` nao fornece idempotencia.
