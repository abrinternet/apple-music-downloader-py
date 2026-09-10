"""Durable metadata for acknowledged deliveries; no media bytes are retained."""
import json
import subprocess
from pathlib import Path


def number(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        return 0


def inspect_delivery(path):
    result = dict(name=Path(path).name, bytes=Path(path).stat().st_size,
                  duration=0, rate=0, bits=0, quality="Desconhecida", message_id=0)
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                               "-show_entries", "stream=codec_name,sample_rate,bits_per_raw_sample:format=duration",
                               "-of", "json", path], capture_output=True, timeout=30, check=True)
        probe = json.loads(proc.stdout)
        result["duration"] = number(probe.get("format", {}).get("duration"))
        streams = probe.get("streams") or []
        if streams:
            stream = streams[0]
            result["rate"] = int(number(stream.get("sample_rate")))
            result["bits"] = int(number(stream.get("bits_per_raw_sample")))
            codec = stream.get("codec_name", "Desconhecida")
            quality = codec
            if codec in ("alac", "flac"):
                quality = "Hi-Res Lossless" if result["rate"] > 48000 or result["bits"] > 16 else "Lossless"
            elif codec == "aac":
                quality = "AAC"
            elif codec == "eac3":
                quality = "Dolby Atmos/Audio"
            result["quality"] = quality
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def classify_failure(err):
    s = str(err).lower()
    categories = [
        (("no space", "disk full"), "Disco cheio: libere espaço no servidor."),
        (("401", "403", "credential", "token"), "Credencial: verifique a sessão e a autorização."),
        (("404", "not available", "not found"), "Catálogo/arquivo indisponível: confira o link e a região."),
        (("too large", "413"), "Limite do Telegram: escolha uma qualidade menor."),
        (("429", "retry after", "too many requests"), "Limite temporário do Telegram: aguarde a retomada automática."),
        (("telegram", "400"), "Telegram: verifique se o bot pode enviar arquivos nesta conversa."),
    ]
    return next((message for terms, message in categories if any(t in s for t in terms)),
                "Falha transitória: nova tentativa automática; /saude verifica os serviços.")


def stats_summary(job):
    from collections import Counter
    rows = [s for k, s in sorted(job.deliveries.items()) if job.sent.get(k) and s["bytes"] > 0]
    text = ""
    if rows:
        largest = max(rows, key=lambda s: s["bytes"])
        smallest = min(rows, key=lambda s: s["bytes"])
        best = max(rows, key=lambda s: (s["bits"], s["rate"]))
        duration = int(sum(s["duration"] for s in rows))
        text = (f"\n📦 Total enviado: {sum(s['bytes'] for s in rows)/2**20:.2f} MiB"
                f"\n🎵 Duração musical conhecida: {duration//3600}h {duration//60%60:02d}min"
                f"\nMaior arquivo: {largest['name']} — {largest['bytes']/2**20:.2f} MiB"
                f"\nMenor arquivo: {smallest['name']} — {smallest['bytes']/2**20:.2f} MiB")
        mid = largest.get("message_id", 0)
        if mid:
            if str(job.chat_id).startswith("-100"):
                text += f"\nMaior arquivo: https://t.me/c/{str(job.chat_id)[4:]}/{mid}"
            else:
                text += f"\nID da mensagem do maior arquivo: {mid}"
        for q, count in sorted(Counter(s["quality"] for s in rows).items()):
            text += f"\n{q}: {count}"
        for rate, count in sorted(Counter(s["rate"] for s in rows).items()):
            if rate:
                text += f"\n{rate/1000:.1f} kHz: {count}"
        if best["bits"]:
            text += f"\nMaior resolução entregue: {best['name']} — {best['bits']}-bit / {best['rate']/1000:.1f} kHz"
    text += f"\n🔄 Tentativas: {job.attempts}. Falhas recuperadas: {job.recovered}."
    text += f"\nTempo de processamento acumulado: {int(job.elapsed_seconds)//60}min."
    missing = len(job.sent) - len(job.deliveries)
    if missing > 0:
        text += f"\nMetadados históricos indisponíveis para {missing} arquivo(s); totais de tamanho/duração são parciais."
    for path, reason in sorted(job.failures.items()):
        if not job.sent.get(path):
            text += f"\nPendente: {Path(path).name} — {reason}"
    return text
