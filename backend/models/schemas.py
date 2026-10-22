"""Pydantic models for API payloads and project/app settings."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class SubtitleStyle(BaseModel):
    preset: Literal["bigword", "classic", "karaoke", "minimal", "animated"] = "bigword"
    font_family: Optional[str] = Field(default=None, max_length=64)
    font_size: Optional[int] = Field(default=None, ge=24, le=200)
    bold: Optional[bool] = None
    text_color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    outline: Optional[float] = Field(default=None, ge=0, le=20)
    shadow: Optional[float] = Field(default=None, ge=0, le=10)
    highlight_color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    position: Optional[float] = Field(default=None, ge=5, le=95)
    animation: Optional[float] = Field(default=None, ge=0, le=1)
    uppercase: Optional[bool] = None
    max_words: Optional[int] = Field(default=None, ge=1, le=8)
    max_line_chars: Optional[int] = Field(default=None, ge=8, le=40)


class Numbering(BaseModel):
    enabled: bool = True
    format: str = Field(default="Часть {n}/{total}", max_length=60)
    position: Literal["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"] = "top-center"
    size: int = Field(default=54, ge=20, le=140)
    margin: int = Field(default=70, ge=10, le=400)

    @field_validator("format")
    @classmethod
    def _needs_n(cls, v: str) -> str:
        if "{n}" not in v:
            raise ValueError("Формат должен содержать {n} (номер части)")
        return v


Layout = Literal["40/60", "50/50", "55/45", "60/40", "65/35", "70/30"]


class Crop(BaseModel):
    # auto: black bars removed + crop window follows the action in each part; manual: positions below
    mode: Literal["auto", "manual"] = "auto"
    # automatic framing style: static — one position per part; follow — re-framed at every scene cut and
    # gliding after the action; dynamic — follow + varied shot sizes (wide / medium / close-up)
    style: Literal["static", "follow", "dynamic"] = "dynamic"
    main_x: float = Field(default=0.5, ge=0, le=1)
    main_y: float = Field(default=0.5, ge=0, le=1)
    main_zoom: float = Field(default=1.0, ge=1, le=3)
    game_x: float = Field(default=0.5, ge=0, le=1)
    game_y: float = Field(default=0.5, ge=0, le=1)
    game_zoom: float = Field(default=1.0, ge=1, le=3)


class Banner(BaseModel):
    """A strip for a banner (picture and/or text): its own space in the frame or drawn over the video."""
    enabled: bool = False
    mode: Literal["reserve", "overlay"] = "reserve"
    position: Literal["top", "middle", "bottom"] = "top"
    height: int = Field(default=240, ge=60, le=700)
    background: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    image_id: Optional[str] = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9]+$")
    text: str = Field(default="", max_length=120)
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    text_size: int = Field(default=72, ge=20, le=200)


class ProjectSettings(BaseModel):
    episode_seconds: float = Field(default=60, ge=5, le=600)
    min_last_seconds: float = Field(default=3, ge=0, le=60)
    layout: Layout = "50/50"
    gameplay_top: bool = False  # gameplay above the cartoon instead of below
    divider: bool = True  # thin black line between the cartoon and the gameplay
    banner: Banner = Field(default_factory=Banner)
    subtitles_enabled: bool = True
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    numbering: Numbering = Field(default_factory=Numbering)
    main_audio: bool = False  # original soundtrack in the output; off = silent parts with subtitles only
    gameplay_audio: bool = False
    gameplay_volume: float = Field(default=0.15, ge=0, le=2)
    gameplay_mode: Literal["continuous", "random"] = "continuous"
    quality: Literal["fast", "balanced", "high"] = "balanced"
    language: Literal["auto", "ru", "en"] = "auto"
    crop: Crop = Field(default_factory=Crop)
    caption_title: str = Field(default="", max_length=100)
    hashtags: str = Field(default="", max_length=500)
    seed: int = 0


class AppSettings(BaseModel):
    defaults: ProjectSettings = Field(default_factory=ProjectSettings)
    whisper_model: str = Field(default="small", max_length=300)
    whisper_device: Literal["auto", "cpu", "cuda"] = "auto"
    encoder: Literal["auto", "cpu", "nvenc", "qsv", "amf"] = "auto"
    max_parallel_renders: int = Field(default=1, ge=1, le=4)
    max_download_mb: int = Field(default=4096, ge=10, le=100000)
    max_upload_mb: int = Field(default=8192, ge=10, le=100000)
    force_normalize: bool = False
    default_hashtags: str = Field(default="#анимация #мультик #роблокс #roblox #рекомендации", max_length=500)
    tiktok_client_key: str = Field(default="", max_length=200)
    tiktok_redirect_uri: str = Field(default="", max_length=500)
    keep_temp_files: bool = False


class MediaUrlIn(BaseModel):
    url: str = Field(max_length=2048)
    kind: Literal["main", "gameplay"]
    # Download only a fragment (seconds), e.g. as much gameplay as the cartoon needs.
    start: Optional[float] = Field(default=None, ge=0, le=86400 * 4)
    end: Optional[float] = Field(default=None, gt=0, le=86400 * 4)


class MediaPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    trim_start: Optional[float] = Field(default=None, ge=0, le=86400 * 4)
    trim_end: Optional[float] = Field(default=None, ge=0, le=86400 * 4)
    reset_trim: bool = False  # use the whole clip again


class ProjectCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    main_media_id: str = Field(max_length=32)
    gameplay_media_ids: list[str] = Field(default_factory=list, max_length=50)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    start: bool = True


class ProjectPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    gameplay_media_ids: Optional[list[str]] = Field(default=None, max_length=50)
    settings: Optional[ProjectSettings] = None


class BuildRequest(BaseModel):
    retranscribe: bool = False
    rerender_all: bool = False


class CueIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(max_length=300)
    words: list[dict] = Field(default_factory=list)


class EpisodeOverrides(BaseModel):
    subtitle_style: Optional[SubtitleStyle] = None
    numbering: Optional[Numbering] = None
    crop: Optional[Crop] = None
    layout: Optional[Layout] = None


class GameplayOverride(BaseModel):
    media_id: str = Field(max_length=32)
    offset: Optional[float] = Field(default=None, ge=0)
    random_offset: bool = False


class EpisodePatch(BaseModel):
    cues: Optional[list[CueIn]] = None
    overrides: Optional[EpisodeOverrides] = None
    reset_overrides: bool = False
    gameplay: Optional[GameplayOverride] = None
    caption: Optional[str] = Field(default=None, max_length=2200)
    hashtags: Optional[str] = Field(default=None, max_length=500)
    scheduled_at: Optional[float] = None
    clear_schedule: bool = False
    publish_order: Optional[int] = None
    publish_mode: Optional[Literal["manual", "api_direct", "api_inbox"]] = None
    render: bool = False


class ApproveIn(BaseModel):
    approved: bool = True


class MarkPublishedIn(BaseModel):
    url: str = Field(default="", max_length=500)
    published: bool = True


class TikTokPublishIn(BaseModel):
    mode: Literal["direct", "inbox"] = "direct"
    privacy_level: str = Field(default="SELF_ONLY", max_length=40)
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False


class TikTokSecretIn(BaseModel):
    client_key: str = Field(max_length=200)
    client_secret: str = Field(default="", max_length=200)
    redirect_uri: str = Field(max_length=500)


class QueueOrderIn(BaseModel):
    episode_ids: list[str] = Field(max_length=2000)
