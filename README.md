# apple-music-downloader-py

Porte em Python do [abrinternet/apple-music-downloader](https://github.com/abrinternet/apple-music-downloader) (Go).
Mesma interface de linha de comando, mesmo `config.yaml`, mesmos protocolos externos (agente Android/Frida para lossless, ffmpeg/MP4Box/mp4decrypt).

## Requisitos

- Python 3.11+
- Binários externos: `ffmpeg`, `MP4Box` (GPAC), `mp4decrypt` (Bento4) — os mesmos da versão Go
- Para lossless (ALAC): o agente `agent.js` rodando via Frida no app Apple Music Android (portas 10020/20020), igual à versão Go

## Instalação

```bash
cd apple-music-downloader-py
python -m venv .venv && .venv/Scripts/activate  # Windows
pip install -e .[dev]
```

## Configuração

Copie `config.yaml.example` para `config.yaml` na pasta de execução e preencha
`media-user-token`. Alternativas: arquivo segredo (`MEDIA_USER_TOKEN_FILE`) ou
variável de ambiente (`MEDIA_USER_TOKEN`).

## Uso

```bash
apple-music-dl <url-do-album>
apple-music-dl --help
apple-music-tgbot  # bot do Telegram (configuração por variáveis de ambiente)
```

## Testes

```bash
pytest
```
