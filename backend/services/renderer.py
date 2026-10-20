"""Rendering of a single episode: subtitle files, ffmpeg composition, verification, thumbnail."""
from __future__ import annotations

import logging
import threading
from fractions import Fraction
from pathlib import Path
from typing import Callable

from backend import config
from backend.errors import UserError
from backend.services import encoders, ffmpeg_utils, gameplay_manager, subtitle_generator, video_composer
from backend.services.video_splitter import EpisodeSpan, cut_window

log = logging.getLogger(__name__)


def project_dir(project_id: str) -> Path:
    if not project_id.isalnum():
        raise ValueError("bad project id")
    return config.DIRS["projects"] / project_id


def episode_basename(idx: int, total: int) -> str:
    width = max(2, len(str(total)))
    return f"Part_{idx + 1:0{width}d}"


def episode_paths(project_id: str, idx: int, total: int) -> dict[str, Path]:
    d = project_dir(project_id) / "episodes"
    base = episode_basename(idx, total)
    return {
        "dir": d,
        "video": d / f"{base}.mp4",
        "tmp": d / f"{base}.rendering.mp4",
        "ass": d / f"{base}.ass",
        "srt": d / f"{base}.srt",
        "thumb": d / f"{base}.jpg",
    }


def effective_options(settings: dict, overrides: dict | None) -> dict:
    """Merge project settings with per-episode overrides."""
    ov = overrides or {}
    style = dict(settings.get("subtitle_style") or {})
    if ov.get("subtitle_style"):
        new = {k: v for k, v in ov["subtitle_style"].items() if v is not None}
        if new.get("preset") and new["preset"] != style.get("preset"):
            # switching preset resets preset-specific defaults unless explicitly overridden
            style = {k: v for k, v in style.items() if k in ("position", "max_words", "max_line_chars")}
        style.update(new)
    numbering = dict(settings.get("numbering") or {})
    numbering.update(ov.get("numbering") or {})
    crop = dict(settings.get("crop") or {})
    crop.update(ov.get("crop") or {})
    layout = ov.get("layout") or settings.get("layout") or "50/50"
    return {"subtitle_style": style, "numbering": numbering, "crop": crop, "layout": layout}


def write_subtitle_files(project: dict, episode: dict, total: int) -> tuple[Path, Path]:
    settings = project["settings"]
    opts = effective_options(settings, episode.get("overrides"))
    paths = episode_paths(project["id"], episode["idx"], total)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    cues = (episode.get("cues") or []) if settings.get("subtitles_enabled", True) else []
    top_h, _ = video_composer.layout_heights(opts["layout"])
    ass = subtitle_generator.build_ass(
        cues, opts["subtitle_style"], opts["numbering"], episode["idx"] + 1, total,
        float(episode["duration"]), top_h,
    )
    tmp = paths["ass"].with_suffix(".ass.tmp")
    tmp.write_text(ass, encoding="utf-8")
    tmp.replace(paths["ass"])
    paths["srt"].write_text(subtitle_generator.build_srt(cues), encoding="utf-8")
    return paths["ass"], paths["srt"]


def verify_output(path: Path, expected_frames: int, fps: Fraction) -> dict:
    """Check the rendered file with ffprobe: streams, codecs, resolution, duration, frame count."""
    data = ffmpeg_utils.ffprobe_json(path)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out_fps = video_composer.output_fps(fps)
    expected_frames_out = expected_frames if out_fps == fps else int(round(expected_frames / fps * out_fps))
    expected_dur = float(Fraction(expected_frames) / fps)
    problems = []
    if v is None:
        problems.append("нет видеодорожки")
    if a is None:
        problems.append("нет аудиодорожки")
    result = {
        "size": path.stat().st_size,
        "expected_frames": expected_frames_out,
        "expected_duration": round(expected_dur, 4),
    }
    if v is not None:
        frames = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None
        vdur = float(v.get("duration") or 0)
        result.update({
            "video_codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
            "frames": frames, "video_duration": round(vdur, 4), "pix_fmt": v.get("pix_fmt"),
        })
        if v.get("codec_name") != "h264":
            problems.append(f"кодек видео {v.get('codec_name')} вместо H.264")
        if (v.get("width"), v.get("height")) != (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT):
            problems.append(f"разрешение {v.get('width')}×{v.get('height')} вместо 1080×1920")
        if frames is not None and abs(frames - expected_frames_out) > 1:
            problems.append(f"{frames} кадров вместо {expected_frames_out}")
        if abs(vdur - expected_dur) > 2.0 / float(out_fps) + 0.01:
            problems.append(f"длительность видео {vdur:.2f} с вместо {expected_dur:.2f} с")
    if a is not None:
        adur = float(a.get("duration") or 0)
        result.update({"audio_codec": a.get("codec_name"), "audio_duration": round(adur, 4),
                       "sample_rate": a.get("sample_rate"), "channels": a.get("channels")})
        if a.get("codec_name") != "aac":
            problems.append(f"кодек звука {a.get('codec_name')} вместо AAC")
        if abs(adur - expected_dur) > 0.1:
            problems.append(f"длительность звука {adur:.2f} с вместо {expected_dur:.2f} с")
    result["ok"] = not problems
    result["problems"] = problems
    return result


def make_thumbnail(video: Path, thumb: Path, at: float) -> None:
    try:
        ffmpeg_utils.run_ffmpeg([
            "-ss", ffmpeg_utils.fmt_time(at), "-i", str(video), "-frames:v", "1",
            "-vf", "scale=360:-2", "-q:v", "4", str(thumb),
        ])
    except Exception as exc:  # thumbnails are cosmetic
        log.warning("thumbnail failed: %s", exc)


def render_episode(
    project: dict,
    episode: dict,
    total: int,
    timeline: dict,
    gameplay_media: dict | None,
    encoder: str,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Render one episode to MP4. Returns DB fields to update."""
    settings = project["settings"]
    opts = effective_options(settings, episode.get("overrides"))
    fps = Fraction(int(timeline["fps_num"]), int(timeline["fps_den"]))
    span = EpisodeSpan(episode["idx"], episode["start_frame"], episode["end_frame"],
                       episode["start_time"], episode["end_time"], episode["duration"])
    window = cut_window(span, fps, float(timeline.get("t0") or 0.0))
    paths = episode_paths(project["id"], episode["idx"], total)
    ass_path, _ = write_subtitle_files(project, episode, total)

    audio_rel = timeline.get("audio_t0")
    audio_delay = 0.0
    if audio_rel is not None:
        audio_delay = max(0.0, float(audio_rel) - (float(timeline.get("t0") or 0.0) + span.start_time))

    gp_path, pieces, gp_audio = None, [], False
    if gameplay_media and gameplay_media.get("path") and Path(gameplay_media["path"]).is_file():
        gp = episode.get("gameplay") or {}
        ustart = float(gameplay_media.get("usable_start") or 0.0)
        uend = float(gameplay_media.get("usable_end") or (gameplay_media.get("info") or {}).get("duration") or 0)
        if uend - ustart > gameplay_manager.MIN_PIECE:
            gp_path = gameplay_media["path"]
            pieces = gameplay_manager.plan_pieces(ustart, uend, float(gp.get("offset") or 0.0), span.duration)
            gp_audio = bool((gameplay_media.get("info") or {}).get("has_audio"))
    elif gameplay_media is not None:
        raise UserError("Файл геймплея не найден. Выберите другой геймплей для этой части.")

    spec = video_composer.EpisodeRenderSpec(
        main_path=timeline["source_path"],
        window=window,
        fps=fps,
        main_has_audio=bool(timeline.get("has_audio")),
        gameplay_path=gp_path,
        gameplay_pieces=pieces,
        gameplay_has_audio=gp_audio,
        gameplay_audio=bool(settings.get("gameplay_audio")),
        gameplay_volume=float(settings.get("gameplay_volume", 0.15)),
        layout=opts["layout"],
        crop=opts["crop"],
        ass_relpath=ass_path.relative_to(config.STORAGE).as_posix(),
        fontsdir_relpath=_fontsdir(),
        audio_delay=audio_delay,
    )
    out_fps = float(video_composer.output_fps(fps))
    venc = encoders.video_encoder_args(encoder, settings.get("quality", "balanced"), out_fps)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    tmp = paths["tmp"]
    cmd = video_composer.build_command(spec, venc, encoders.AUDIO_ARGS, str(tmp))
    try:
        ffmpeg_utils.run_ffmpeg(cmd, duration=span.duration, on_progress=on_progress,
                                cancel_event=cancel_event, cwd=config.STORAGE,
                                error_message=f"Не удалось создать часть {episode['idx'] + 1}.")
    except ffmpeg_utils.FFmpegError:
        if encoder != "cpu":
            log.warning("hardware encoder %s failed, retrying on CPU", encoder)
            venc = encoders.video_encoder_args("cpu", settings.get("quality", "balanced"), out_fps)
            cmd = video_composer.build_command(spec, venc, encoders.AUDIO_ARGS, str(tmp))
            ffmpeg_utils.run_ffmpeg(cmd, duration=span.duration, on_progress=on_progress,
                                    cancel_event=cancel_event, cwd=config.STORAGE,
                                    error_message=f"Не удалось создать часть {episode['idx'] + 1}.")
        else:
            tmp.unlink(missing_ok=True)
            raise
    verify = verify_output(tmp, span.frames, fps)
    if not verify["ok"]:
        tmp.unlink(missing_ok=True)
        raise UserError("Проверка результата не пройдена: " + "; ".join(verify["problems"]))
    tmp.replace(paths["video"])
    make_thumbnail(paths["video"], paths["thumb"], min(1.0, span.duration / 2))
    return {
        "video_path": str(paths["video"]),
        "thumb_path": str(paths["thumb"]) if paths["thumb"].is_file() else None,
        "verify": verify,
    }


def _fontsdir() -> str | None:
    d = config.STORAGE / "fonts"
    if d.is_dir() and any(p.suffix.lower() in (".ttf", ".otf") for p in d.iterdir()):
        return "fonts"
    return None
