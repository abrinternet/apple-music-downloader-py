"""Apple Music API response models.

Port of the response structs in utils/ampapi/*.go and utils/structs/structs.go.
Pydantic maps camelCase JSON keys onto snake_case attributes automatically;
unknown fields are ignored, mirroring Go's json.Unmarshal behaviour.
"""

from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model accepting camelCase JSON keys and snake_case Python attrs."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Artwork(CamelModel):
    width: int = 0
    height: int = 0
    url: str = ""
    bg_color: str = ""
    text_color1: str = ""
    text_color2: str = ""
    text_color3: str = ""
    text_color4: str = ""


class Preview(CamelModel):
    url: str = ""


class PlayParams(CamelModel):
    id: str = ""
    kind: str = ""
    format: str = ""
    station_hash: str = ""


class MotionVideo(CamelModel):
    video: str = ""


class EditorialVideo(CamelModel):
    motion_tall: MotionVideo = Field(default_factory=MotionVideo, alias="motionTallVideo3x4")
    motion_square: MotionVideo = Field(default_factory=MotionVideo, alias="motionSquareVideo1x1")
    motion_detail_tall: MotionVideo = Field(default_factory=MotionVideo, alias="motionDetailTall")
    motion_detail_square: MotionVideo = Field(
        default_factory=MotionVideo, alias="motionDetailSquare"
    )


class ExtendedAssetUrls(CamelModel):
    enhanced_hls: str = ""


class SongAttributes(CamelModel):
    """Shared by songs, music videos and the artist-album listing shape."""

    previews: list[Preview] = Field(default_factory=list)
    artwork: Artwork = Field(default_factory=Artwork)
    artist_name: str = ""
    url: str = ""
    disc_number: int = 0
    genre_names: list[str] = Field(default_factory=list)
    has_time_synced_lyrics: bool = False
    is_mastered_for_itunes: bool = False
    is_apple_digital_master: bool = False
    content_rating: Optional[str] = None
    duration_in_millis: int = 0
    release_date: str = ""
    name: str = ""
    extended_asset_urls: ExtendedAssetUrls = Field(default_factory=ExtendedAssetUrls)
    isrc: str = ""
    audio_traits: list[str] = Field(default_factory=list)
    has_lyrics: bool = False
    album_name: str = ""
    play_params: PlayParams = Field(default_factory=PlayParams)
    track_number: int = 0
    audio_locale: str = ""
    composer_name: str = ""

    def __hash__(self) -> int:  # allow hashing like Go struct values
        return hash((self.name, self.isrc, self.url))


class ArtistRefAttributes(CamelModel):
    name: str = ""


class ArtistRef(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: ArtistRefAttributes = Field(default_factory=ArtistRefAttributes)


class ArtistWithArtworkAttributes(CamelModel):
    name: str = ""
    artwork: Artwork = Field(default_factory=Artwork)


class ArtistWithArtwork(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: ArtistWithArtworkAttributes = Field(
        default_factory=ArtistWithArtworkAttributes
    )


class AlbumMiniAttributes(CamelModel):
    artist_name: str = ""
    artwork: Artwork = Field(default_factory=Artwork)
    genre_names: list[str] = Field(default_factory=list)
    is_compilation: bool = False
    is_complete: bool = False
    is_mastered_for_itunes: bool = False
    is_prerelease: bool = False
    is_single: bool = False
    name: str = ""
    play_params: PlayParams = Field(default_factory=PlayParams)
    release_date: str = ""
    track_count: int = 0
    upc: str = ""
    url: str = ""


class AlbumMini(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: AlbumMiniAttributes = Field(default_factory=AlbumMiniAttributes)


_T = TypeVar("_T")


class Relationship(CamelModel, Generic[_T]):
    """A paginated relationship block: ``href``/``next`` plus resource list."""

    href: str = ""
    next: str = ""
    data: list[_T] = Field(default_factory=list)


class TrackRelationships(CamelModel):
    artists: Relationship[ArtistRef] = Field(
        default_factory=lambda: Relationship[ArtistRef]()
    )
    albums: Relationship[AlbumMini] = Field(
        default_factory=lambda: Relationship[AlbumMini]()
    )


class TrackResource(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: SongAttributes = Field(default_factory=SongAttributes)
    relationships: TrackRelationships = Field(default_factory=TrackRelationships)


class CollectionAttributes(CamelModel):
    """Attributes of an album or playlist resource."""

    artwork: Artwork = Field(default_factory=Artwork)
    artist_name: str = ""
    is_single: bool = False
    url: str = ""
    is_complete: bool = False
    genre_names: list[str] = Field(default_factory=list)
    track_count: int = 0
    is_mastered_for_itunes: bool = False
    is_apple_digital_master: bool = False
    content_rating: Optional[str] = None
    release_date: str = ""
    name: str = ""
    record_label: str = ""
    upc: str = ""
    audio_traits: list[str] = Field(default_factory=list)
    copyright: str = ""
    play_params: PlayParams = Field(default_factory=PlayParams)
    is_compilation: bool = False
    editorial_video: EditorialVideo = Field(default_factory=EditorialVideo)


class RecordLabelsRel(CamelModel):
    href: str = ""
    data: list[Any] = Field(default_factory=list)


class CollectionRelationships(CamelModel):
    record_labels: RecordLabelsRel = Field(default_factory=RecordLabelsRel)
    artists: Relationship[ArtistWithArtwork] = Field(
        default_factory=lambda: Relationship[ArtistWithArtwork]()
    )
    tracks: Relationship[TrackResource] = Field(
        default_factory=lambda: Relationship[TrackResource]()
    )


class CollectionResource(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: CollectionAttributes = Field(default_factory=CollectionAttributes)
    relationships: CollectionRelationships = Field(
        default_factory=CollectionRelationships
    )


class StationAttributes(CamelModel):
    artwork: Artwork = Field(default_factory=Artwork)
    is_live: bool = False
    url: str = ""
    name: str = ""
    editorial_video: EditorialVideo = Field(default_factory=EditorialVideo)
    play_params: PlayParams = Field(default_factory=PlayParams)


class StationResource(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: StationAttributes = Field(default_factory=StationAttributes)


class Response(CamelModel, Generic[_T]):
    href: str = ""
    next: str = ""
    data: list[_T] = Field(default_factory=list)


# Concrete aliases mirroring the Go type names.
SongResp = Response[TrackResource]
TrackResp = Response[TrackResource]
AlbumResp = Response[CollectionResource]
PlaylistResp = Response[CollectionResource]
MusicVideoResp = Response[TrackResource]
StationResp = Response[StationResource]


# --- Search -----------------------------------------------------------------


class SearchResultAttributes(CamelModel):
    name: str = ""
    genre_names: list[str] = Field(default_factory=list)
    url: str = ""


class SearchResultResource(CamelModel):
    id: str = ""
    type: str = ""
    href: str = ""
    attributes: SearchResultAttributes = Field(default_factory=SearchResultAttributes)


class SearchResults(CamelModel):
    songs: Optional[Response[TrackResource]] = None
    albums: Optional[Response[CollectionResource]] = None
    artists: Optional[Response[SearchResultResource]] = None


class SearchResp(CamelModel):
    results: SearchResults = Field(default_factory=SearchResults)


# --- Station assets ----------------------------------------------------------


class StationAsset(CamelModel):
    key_server_url: str = ""
    url: str = ""
    widevine_key_certificate_url: str = ""
    fair_play_key_certificate_url: str = ""


class StationAssetsResults(CamelModel):
    assets: list[StationAsset] = Field(default_factory=list)


class StationAssets(CamelModel):
    results: StationAssetsResults = Field(default_factory=StationAssetsResults)


# --- Artist catalog listing (structs.go AutoGeneratedArtist) -----------------

AutoGeneratedArtist = Response[TrackResource]
