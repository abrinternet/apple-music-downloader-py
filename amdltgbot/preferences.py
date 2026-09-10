import json
import os
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse


class Preferences:
    def preference_path(self, user_id):
        return Path(self.cfg.download_root) / ".preferences" / f"{int(user_id)}.json"

    def load_preferences(self, session):
        try:
            data = json.loads(self.preference_path(session.user_id).read_text())
            choices = {"format": ("alac", "aac", "atmos"), "alac_max": (44100, 48000, 96000, 192000),
                       "atmos_max": (2448, 2768), "aac_type": ("aac-lc", "aac-binaural", "aac-downmix")}
            for key, values in choices.items():
                if data.get(key) in values:
                    setattr(session, key, data[key])
        except (OSError, ValueError):
            pass

    def save_preferences(self, session):
        path = self.preference_path(session.user_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix="preferences-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({key: getattr(session, key) for key in ("format", "alac_max", "atmos_max", "aac_type")}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def health_report(self, chat_id, user_id):
        text = "🩺 Saúde dos serviços"
        for name, key, default in [
            ("Wrapper: conta", "WRAPPER_ACCOUNT_URL", "http://127.0.0.1:30020/"),
            ("Wrapper: áudio", "WRAPPER_DECRYPT_ADDRESS", "127.0.0.1:10020"),
            ("Wrapper: catálogo", "WRAPPER_M3U8_ADDRESS", "127.0.0.1:20020"),
        ]:
            raw = os.getenv(key, default)
            u = urlparse(raw if "://" in raw else "tcp://" + raw)
            try:
                with socket.create_connection((u.hostname, u.port), timeout=2):
                    status = "acessível"
            except (OSError, ValueError):
                status = "indisponível"
            text += f"\n{name}: {status}"
        try:
            self.api.send_chat_action(chat_id, "typing")
            text += "\nTelegram: conectado"
        except Exception:
            text += "\nTelegram: falha de comunicação"
        try:
            text += f"\nEspaço livre: {shutil.disk_usage(self.cfg.download_root).free/2**30:.2f} GiB"
        except OSError:
            text += "\nEspaço livre: indisponível"
        text += f"\nPedidos na fila: {self.queue.qsize()}"
        if str(user_id) in [x.strip() for x in os.getenv("TELEGRAM_ADMIN_USERS", "").split(",")]:
            text += "\nDiagnóstico administrativo: " + self.status_text()
        self.api.send_message(chat_id, text)
