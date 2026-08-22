"""Album/playlist/station orchestration.

Port of utils/task/{track,album,playlist,station}.go.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ampapi.api import (
    get_album_resp,
    get_album_resp_by_href,
    get_playlist_resp,
    get_station_next_tracks,
    get_station_resp,
)
from .ampapi.models import CollectionAttributes, CollectionResource, TrackResource
from .state import State
from .ui import console, render_table


@dataclass
class Track:
    id: str = ""
    type: str = ""
    name: str = ""
    storefront: str = ""
    language: str = ""

    save_dir: str = ""
    save_name: str = ""
    save_path: str = ""
    codec: str = ""
    task_num: int = 0
    task_total: int = 0
    m3u8: str = ""
    web_m3u8: str = ""
    device_m3u8: str = ""
    quality: str = ""
    cover_path: str = ""

    resp: TrackResource | None = None
    pre_type: str = ""  # parent kind: albums / playlists / stations
    pre_id: str = ""  # parent ID
    disc_total: int = 0
    album_data: CollectionResource | None = None
    playlist_data: CollectionResource | None = None

    def get_album_data(self, state: State, token: str) -> None:
        resp = get_album_resp_by_href(self.resp.href, self.language, token)
        self.album_data = resp.data[0]
        # Try to learn the total number of discs from the album's track list.
        tracks = resp.data[0].relationships.tracks.data
        if tracks:
            self.disc_total = tracks[-1].attributes.disc_number


def _parse_selection(input_text: str, total_options: int) -> list[int]:
    """Parse '1,3-5,all' style input exactly like the Go helpers."""
    arr = list(range(1, total_options + 1))
    if input_text == "all":
        print("You have selected all options:")
        return arr
    selected: list[int] = []
    parts = input_text.split(",")
    for part in parts:
        if "-" in part:  # range setting
            start_s, _, end_s = part.partition("-")
            opts: tuple[str, ...] = (start_s, end_s)
        else:
            opts = (part,)
        if len(opts) == 1:
            try:
                num = int(opts[0])
            except ValueError:
                print("Invalid option:", opts[0])
                continue
            if 0 < num <= total_options:
                selected.append(num)
            else:
                print("Option out of range:", opts[0])
        elif len(opts) == 2:
            try:
                start, end = int(opts[0]), int(opts[1])
            except ValueError:
                print("Invalid range:", part)
                continue
            if start < 1 or end > total_options or start > end:
                print("Range out of range:", part)
                continue
            selected.extend(range(start, end + 1))
        else:
            print("Invalid option:", part)
    return selected


def _prompt_selection() -> str:
    console.print(
        "Please select from the track options above "
        "(multiple options separated by commas, ranges supported, or type 'all' to select all)"
    )
    console.print("select: ", end="")
    try:
        return input().strip()
    except (KeyboardInterrupt, EOFError):
        return ""


class Album:
    def __init__(self, state: State, storefront: str, album_id: str) -> None:
        self.state = state
        self.storefront = storefront
        self.id = album_id

        self.save_dir = ""
        self.save_name = ""
        self.codec = ""
        self.cover_path = ""

        self.language = ""
        self.resp = None
        self.name = ""
        self.tracks: list[Track] = []

    def get_resp(self, token: str, language: str) -> None:
        self.language = language
        try:
            resp = get_album_resp(self.storefront, self.id, self.language, token)
        except Exception as exc:
            raise RuntimeError(f"error getting album response: {exc}") from exc
        self.resp = resp
        self.name = resp.data[0].attributes.name
        tracks = resp.data[0].relationships.tracks.data
        for i, track_data in enumerate(tracks):
            self.tracks.append(
                Track(
                    id=track_data.id,
                    type=track_data.type,
                    name=track_data.attributes.name,
                    language=self.language,
                    storefront=self.storefront,
                    task_num=i + 1,
                    task_total=len(tracks),
                    m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                    web_m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                    resp=track_data,
                    pre_type="albums",
                    disc_total=tracks[-1].attributes.disc_number,
                    pre_id=self.id,
                    album_data=resp.data[0],
                )
            )

    def show_select(self) -> list[int]:
        meta = self.resp
        tracks = meta.data[0].relationships.tracks.data
        rows = []
        for num, track in enumerate(tracks, start=1):
            track_name = f"{track.attributes.track_number:02d}. {track.attributes.name}"
            rating = track.attributes.content_rating or ""
            rows.append([str(num), track_name, rating, track.type])

        caption = (
            f"Storefront: {self.storefront.upper()}, "
            f"{meta.data[0].attributes.track_count - len(tracks)} tracks missing"
        )
        render_table(["", "Track Name", "Rating", "Type"], rows, caption=caption)

        input_text = _prompt_selection()
        return _parse_selection(input_text, len(tracks))


class Playlist:
    def __init__(self, state: State, storefront: str, playlist_id: str) -> None:
        self.state = state
        self.storefront = storefront
        self.id = playlist_id

        self.save_dir = ""
        self.save_name = ""
        self.codec = ""
        self.cover_path = ""

        self.language = ""
        self.resp = None
        self.name = ""
        self.tracks: list[Track] = []

    def get_resp(self, token: str, language: str) -> None:
        self.language = language
        try:
            resp = get_playlist_resp(self.storefront, self.id, self.language, token)
        except Exception as exc:
            raise RuntimeError(f"error getting album response: {exc}") from exc
        self.resp = resp
        resp.data[0].attributes.artist_name = "Apple Music"
        self.name = resp.data[0].attributes.name
        tracks = resp.data[0].relationships.tracks.data
        for i, track_data in enumerate(tracks):
            self.tracks.append(
                Track(
                    id=track_data.id,
                    type=track_data.type,
                    name=track_data.attributes.name,
                    language=self.language,
                    storefront=self.storefront,
                    task_num=i + 1,
                    task_total=len(tracks),
                    m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                    web_m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                    resp=track_data,
                    pre_type="playlists",
                    # DiscTotal is fetched elsewhere for playlists.
                    pre_id=self.id,
                    playlist_data=resp.data[0],
                )
            )

    def show_select(self) -> list[int]:
        meta = self.resp
        tracks = meta.data[0].relationships.tracks.data
        rows = []
        for num, track in enumerate(tracks, start=1):
            track_name = f"{track.attributes.name} - {track.attributes.artist_name}"
            rating = track.attributes.content_rating or ""
            rows.append([str(num), track_name, rating, track.type])
        rows = [[c or "None" for c in row] for row in rows]
        for row in rows:
            row[2] = {"explicit": "E", "clean": "C"}.get(row[2], "None")
            row[3] = {"music-videos": "MV", "songs": "SONG"}.get(row[3], row[3])

        render_table(
            ["", "Track Name", "Rating", "Type"],
            rows,
            caption=f"Playlists: {len(tracks)} tracks",
        )
        input_text = _prompt_selection()
        return _parse_selection(input_text, len(tracks))


class Station:
    def __init__(self, state: State, storefront: str, station_id: str) -> None:
        self.state = state
        self.storefront = storefront
        self.id = station_id

        self.save_dir = ""
        self.save_name = ""
        self.codec = ""
        self.cover_path = ""

        self.language = ""
        self.resp = None
        self.type = ""
        self.name = ""
        self.tracks: list[Track] = []

    def get_resp(self, mutoken: str, token: str, language: str) -> None:
        self.language = language
        try:
            resp = get_station_resp(self.storefront, self.id, self.language, token)
        except Exception as exc:
            raise RuntimeError(f"error getting station response: {exc}") from exc
        self.resp = resp
        self.type = resp.data[0].attributes.play_params.format
        self.name = resp.data[0].attributes.name
        if self.type != "tracks":
            return
        try:
            tracks_resp = get_station_next_tracks(self.id, mutoken, self.language, token)
        except Exception as exc:
            raise RuntimeError(f"error getting station tracks response: {exc}") from exc

        for i, track_data in enumerate(tracks_resp.data):
            try:
                album_resp = get_album_resp_by_href(track_data.href, self.language, token)
            except Exception as exc:
                print("Error getting album response:", exc)
                continue
            album_tracks = album_resp.data[0].relationships.tracks.data
            track = Track(
                id=track_data.id,
                type=track_data.type,
                name=track_data.attributes.name,
                language=self.language,
                storefront=self.storefront,
                task_num=i + 1,
                task_total=len(tracks_resp.data),
                m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                web_m3u8=track_data.attributes.extended_asset_urls.enhanced_hls,
                resp=track_data,
                pre_type="stations",
                disc_total=album_tracks[-1].attributes.disc_number if album_tracks else 0,
                pre_id=self.id,
                album_data=album_resp.data[0],
            )
            track.playlist_data = CollectionResource(
                attributes=CollectionAttributes(name=self.name)
            )
            track.playlist_data.attributes.artist_name = "Apple Music Station"
            self.tracks.append(track)

    def get_artwork_url(self) -> str:
        return self.resp.data[0].attributes.artwork.url
