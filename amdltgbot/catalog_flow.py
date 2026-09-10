"""Telegram selection flow, separated from the download worker."""
from .catalog import Catalog, item_url, select_numbers, preview
from .interactive import keyboard, detect_media_kind


class CatalogFlow:
    def catalog_rows(self, s):
        rows = []
        for url in s.urls:
            for item in Catalog().items(url, s.single_song):
                if item.get("type") == "albums" and s.has_kind("artist"):
                    rows.extend(Catalog().items(item_url(item)))
                else:
                    rows.append(item)
        return rows

    def search_page(self, s, offset=0):
        try:
            s.catalog_items, s.catalog_next = Catalog().search(s.search_type, s.search_query, offset)
            s.catalog_offset = offset
            s.catalog_page = 0
            s.step = "catalog_search"
            self.show_catalog(s)
        except Exception:
            self.api.send_message(s.chat_id, "Não foi possível consultar o catálogo. Verifique /saude e tente /buscar novamente.")

    def start_selection(self, s, albums=False):
        try:
            s.catalog_items = (Catalog().items(s.urls[0]) if albums else self.catalog_rows(s))
            s.catalog_page = 0
            s.catalog_selected = set()
            s.step = "catalog_albums" if albums else "catalog_tracks"
            self.show_catalog(s)
        except Exception:
            self.api.send_message(s.chat_id, "Não foi possível carregar os itens. Verifique /saude e tente novamente.")

    def show_catalog(self, s):
        search = s.step == "catalog_search"
        start = 0 if search else s.catalog_page * 10
        rows = s.catalog_items[start:start + 10]
        text = "🔎 Resultados" if search else "Escolha os itens pelos botões ou envie números: 1,3,7-12."
        buttons = []
        for i, item in enumerate(rows, start):
            a = item.get("attributes", {})
            title = f"{a.get('name', 'Sem título')} — {a.get('artistName', '')}"
            album = a.get("albumName", "")
            text += f"\n{i+1}. {title}" + (f" · {album}" if album else "")
            artwork = a.get("artwork", {}).get("url", "")
            mark = "✅ " if i in s.catalog_selected else ""
            buttons.append([(f"{mark}{i+1}. {title}"[:60], f"cat:item:{i}")])
        if not rows:
            text += "\nNenhum resultado."
        nav = []
        page = s.catalog_offset if search else s.catalog_page
        if page > 0:
            nav.append(("⬅️ Anterior", "cat:prev"))
        if s.catalog_next if search else start + 10 < len(s.catalog_items):
            nav.append(("Próxima ➡️", "cat:next"))
        if nav:
            buttons.append(nav)
        if not search:
            buttons.append([(f"Confirmar seleção ({len(s.catalog_selected)})", "cat:done")])
        buttons.append([("Cancelar", "cfm:no")])
        kb = keyboard(buttons)
        for position, item in enumerate(rows):
            artwork = item.get("attributes", {}).get("artwork", {}).get("url", "")
            if artwork.startswith("https://"):
                kb["inline_keyboard"][position].append({"text":"🖼 Capa", "url":artwork.replace("{w}", "300").replace("{h}", "300")})
        self.send_or_edit_menu(s, text[:3900], kb)

    def catalog_callback(self, s, data):
        if data in ("cat:next", "cat:prev"):
            delta = 1 if data.endswith("next") else -1
            if s.step == "catalog_search":
                self.catalog_async(s, lambda copy: self.search_page(copy, max(0, copy.catalog_offset + delta * 10)))
            else:
                s.catalog_page = max(0, min((len(s.catalog_items)-1)//10, s.catalog_page + delta))
                self.show_catalog(s)
        elif data.startswith("cat:item:"):
            try:
                index = int(data.rsplit(":", 1)[1])
                if not 0 <= index < len(s.catalog_items):
                    return
                if s.step == "catalog_search":
                    self.start_interactive_flow(s.chat_id, s.user_id, [item_url(s.catalog_items[index])])
                else:
                    if index in s.catalog_selected:
                        s.catalog_selected.remove(index)
                    else:
                        s.catalog_selected.add(index)
                    self.show_catalog(s)
            except ValueError:
                return
        elif data == "cat:done":
            self.finish_selection(s)

    def selection_text(self, s, text):
        try:
            s.catalog_selected = set(select_numbers(text, len(s.catalog_items)))
            self.show_catalog(s)
        except ValueError as exc:
            self.api.send_message(s.chat_id, str(exc))

    def finish_selection(self, s):
        if not s.catalog_selected:
            self.api.send_message(s.chat_id, "Selecione pelo menos um item.")
            return
        items = [s.catalog_items[i] for i in sorted(s.catalog_selected)]
        s.urls = [item_url(x) for x in items]
        s.kinds = [detect_media_kind(x) for x in s.urls]
        s.single_song = all(x == "song" for x in s.kinds)
        s.select_tracks = s.all_album = s.is_search = False
        s.preview_text = ""
        self.advance_to_content_options(s)

    def prepare_preview(self, s):
        if not s.preview_text:
            try:
                rows = self.catalog_rows(s)
                s.preview_text = preview(rows, s.format)
            except Exception:
                s.preview_text = "\nPrévia indisponível: não foi possível consultar o catálogo. Você pode cancelar ou confirmar o link original."
        return s.preview_text

    def catalog_async(self, s, fn):
        import copy
        import threading
        target = copy.deepcopy(s)
        target.source = s
        if self.catalog_dispatch:
            self.catalog_dispatch(lambda: fn(target))
            target.source = None
            s.__dict__.update(target.__dict__)
            return
        s.step = "catalog_loading"
        self.api.send_message(s.chat_id, "🔎 Consultando o catálogo… /status e /cancel continuam disponíveis.")
        def work():
            try:
                fn(target)
            finally:
                self.publish_catalog(target)
        threading.Thread(target=work, daemon=True).start()

    def publish_catalog(self, s):
        source = getattr(s, "source", None)
        if source is None or self.catalog_dispatch:
            return True
        with self.sessions_lock:
            if self.sessions.get(s.chat_id) is not source:
                return False
            s.source = None
            self.sessions[s.chat_id] = s
            return True
