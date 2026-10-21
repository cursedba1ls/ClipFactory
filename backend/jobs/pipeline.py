"""Job handlers: media import, full series build, episode re-render, TikTok publishing."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

from backend import config
from backend.database import db
from backend.errors import Cancelled, UserError
from backend.jobs.queue import JobContext, register
from backend.services import (
    captions, downloader, encoders, ffmpeg_utils, gameplay_manager, publisher, renderer,
    settings_store, smart_crop, subtitle_generator, subtitle_import, video_analyzer, video_splitter,
)

log = logging.getLogger(__name__)

# Indirection so tests can substitute a fake speech recogniser.
TRANSCRIBE = subtitle_generator.transcribe

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(50), Fraction(60000, 1001), Fraction(60)]


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def process_media(media_id: str, ctx: JobContext | None, p0: float = 0, p1: float = 100) -> dict:
    """Download (if needed) and analyse a media item. Idempotent."""
    media = db.get("media", media_id)
    if not media:
        raise UserError("Видео не найдено в библиотеке.")
    if media["status"] == "ready" and media.get("path") and Path(media["path"]).is_file() and media.get("info"):
        return media
    app = settings_store.get_settings()
    cancel = ctx.cancel_event if ctx else None
    kind_name = "основного видео" if media["kind"] == "main" else "геймплея"
    try:
        path = Path(media["path"]) if media.get("path") else None
        if (path is None or not path.is_file()) and media.get("source_url"):
            db.update("media", media_id, {"status": "downloading", "error": None})
            dest = config.DIRS["downloads"] if media["kind"] == "main" else config.DIRS["gameplay"]

            def on_dl(frac: float, msg: str) -> None:
                if ctx:
                    ctx.update(stage="Загрузка видео", progress=p0 + (p1 - p0) * 0.9 * frac,
                               message=f"Скачивание {kind_name}: {msg}")

            if ctx:
                ctx.update(stage="Загрузка видео", progress=p0, message=f"Скачивание {kind_name}…", force=True)
            path, title = downloader.download_url(media["source_url"], dest, media_id, app.max_download_mb,
                                                  on_dl, cancel)
            db.update("media", media_id, {"path": str(path), "title": media.get("title") or title or None})
        if path is None or not path.is_file():
            raise UserError("Файл видео не найден. Загрузите его заново.")
        if ctx:
            ctx.update(stage="Анализ исходника", progress=p0 + (p1 - p0) * 0.92,
                       message=f"Анализ {kind_name}…", force=True)
        info = video_analyzer.analyze(path)
        values = {"info": info, "status": "ready", "error": None}
        us, ue = 0.0, None
        if media["kind"] == "gameplay":
            us, ue = video_analyzer.detect_black_edges(path, info["video_duration"])
            values.update({"usable_start": us, "usable_end": ue})
        if ctx:
            ctx.update(message=f"Поиск чёрных полос и области действия ({kind_name})…")
        info.update(smart_crop.analyze_media(path, info, media["kind"], us, ue))
        db.update("media", media_id, values)
    except Cancelled:
        db.update("media", media_id, {"status": "pending"})
        raise
    except UserError as exc:
        db.update("media", media_id, {"status": "error", "error": exc.message})
        raise
    return db.get("media", media_id)


@register("download", "io", "Загрузка видео")
def job_download(ctx: JobContext) -> None:
    process_media(ctx.job["media_id"], ctx, 0, 100)


# ---------------------------------------------------------------------------
# Series build
# ---------------------------------------------------------------------------

def _target_rate(fps: float) -> Fraction:
    for r in STANDARD_RATES:
        if abs(float(r) - fps) / float(r) < 0.03:
            return r
    return Fraction(max(1, min(60, int(round(fps)))))


def prepare_timeline(project: dict, media: dict, ctx: JobContext, p0: float, p1: float) -> dict:
    """Pick the source used for cutting; normalise VFR input to constant frame rate first."""
    app = settings_store.get_settings()
    info = media["info"]
    pdir = renderer.project_dir(project["id"])
    pdir.mkdir(parents=True, exist_ok=True)
    source = Path(media["path"])
    normalized = False
    if info.get("is_vfr") or app.force_normalize:
        out = pdir / "normalized.mkv"
        rate = _target_rate(float(info["fps"]))
        if not out.is_file():
            ctx.update(stage="Анализ исходника", progress=p0,
                       message="Переменная частота кадров: выравнивание таймлайна…", force=True)
            tmp = pdir / "normalized.tmp.mkv"
            args = ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", f"fps={rate.numerator}/{rate.denominator}", "-fps_mode", "cfr",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                    "-g", str(int(round(float(rate)))), "-pix_fmt", "yuv420p",
                    "-af", "aresample=async=1000:first_pts=0", "-c:a", "pcm_s16le", "-ar", "48000",
                    str(tmp)]
            ffmpeg_utils.run_ffmpeg(
                args, duration=info["duration"],
                on_progress=lambda f: ctx.update(progress=p0 + (p1 - p0) * f),
                cancel_event=ctx.cancel_event, error_message="Не удалось нормализовать частоту кадров видео.")
            tmp.replace(out)
        source = out
        normalized = True
        info = video_analyzer.analyze(source)
    fps = Fraction(int(info["fps_num"]), int(info["fps_den"]))
    total = info.get("nb_frames") or int(round(info["video_duration"] * float(fps)))
    fstart = info.get("format_start") or 0.0
    t0 = max(0.0, (info.get("video_start") or fstart) - fstart)
    audio_t0 = None
    if info.get("has_audio") and info.get("audio_start") is not None:
        audio_t0 = max(0.0, info["audio_start"] - fstart)
    timeline = {
        "source_path": str(source),
        "normalized": normalized,
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "fps": float(fps),
        "total_frames": int(total),
        "duration": float(Fraction(int(total)) / fps),
        "t0": t0,
        "audio_t0": audio_t0,
        "has_audio": bool(info.get("has_audio")),
        "width": info["width"],
        "height": info["height"],
        "content_box": (media.get("info") or {}).get("content_box"),
    }
    db.update("projects", project["id"], {"timeline": timeline, "updated_at": db.now()})
    return timeline


def ensure_episodes(project: dict, timeline: dict) -> list[dict]:
    settings = project["settings"]
    fps = Fraction(timeline["fps_num"], timeline["fps_den"])
    spans = video_splitter.plan_episodes(timeline["total_frames"], fps, float(settings["episode_seconds"]),
                                         float(settings.get("min_last_seconds", 3)))
    video_splitter.validate_coverage(spans, timeline["total_frames"])
    existing = db.query("episodes", "project_id = ?", (project["id"],), "idx")
    same = len(existing) == len(spans) and all(
        e["start_frame"] == s.start_frame and e["end_frame"] == s.end_frame for e, s in zip(existing, spans))
    if same:
        return existing
    # Plan changed (new duration or new source): drop old episodes and their files.
    if existing:
        shutil.rmtree(renderer.project_dir(project["id"]) / "episodes", ignore_errors=True)
        db.connect().execute("DELETE FROM episodes WHERE project_id = ?", (project["id"],))
    t = db.now()
    for s in spans:
        db.insert("episodes", {
            "id": db.new_id(), "project_id": project["id"], "idx": s.index,
            "start_frame": s.start_frame, "end_frame": s.end_frame,
            "start_time": s.start_time, "end_time": s.end_time, "duration": s.duration,
            "status": "pending", "subtitle_status": "none", "cues": [], "overrides": {},
            "publish_order": s.index, "updated_at": t,
        })
    return db.query("episodes", "project_id = ?", (project["id"],), "idx")


def extract_audio(source: str, out: Path, duration: float, ctx: JobContext, p0: float, p1: float) -> None:
    tmp = out.with_suffix(".tmp.wav")
    ffmpeg_utils.run_ffmpeg(
        ["-i", source, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
        duration=duration, on_progress=lambda f: ctx.update(progress=p0 + (p1 - p0) * f),
        cancel_event=ctx.cancel_event, error_message="Не удалось извлечь звук для распознавания речи.")
    tmp.replace(out)


def fmt_span(seconds: float) -> str:
    return video_splitter.format_timecode(float(seconds))


def ensure_transcript(project: dict, timeline: dict, episodes: list[dict], ctx: JobContext,
                      p0: float, p1: float, force: bool = False, rebuild: bool = False) -> dict | None:
    """Transcribe once (cached) and fill episode cues.

    force   — run speech recognition again and replace all cues (including edited ones);
    rebuild — regenerate non-edited cues from the existing transcript (e.g. after style changes).
    """
    settings = project["settings"]
    if not settings.get("subtitles_enabled", True):
        db.connect().execute("UPDATE episodes SET subtitle_status = 'disabled' WHERE project_id = ?",
                             (project["id"],))
        return None
    rebuild_states = ("none", "disabled", "auto", "empty") if rebuild else ("none", "disabled")
    external = subtitle_import.load_external(project["main_media_id"]) if project.get("main_media_id") else None
    if external:
        # A subtitle file replaces speech recognition. Its times count from the start of the file;
        # the episode timeline starts at the first video frame.
        ctx.update(stage="Распознавание речи", progress=p1,
                   message=f"Субтитры из файла «{external.get('filename', '')}» — распознавание не нужно", force=True)
        offset = -float(timeline.get("t0") or 0.0)
        transcript = {"language": None, "model": "file", "source": "file", "segments": [],
                      "words": [{**w, "start": round(w["start"] + offset, 3), "end": round(w["end"] + offset, 3)}
                                for w in external["words"]]}
        tpath = renderer.project_dir(project["id"]) / "transcript.json"
        subtitle_generator.save_json(tpath, transcript)
        db.update("projects", project["id"], {"transcript_path": str(tpath), "transcript_language": "file"})
        pending = [e for e in episodes if e["subtitle_status"] in rebuild_states or force]
        _assign_cues(project, episodes, transcript, pending)
        return transcript
    if not timeline.get("has_audio"):
        _add_warning(project["id"], "В основном видео нет звуковой дорожки — субтитры не созданы, "
                                    "в части добавлена тишина.")
        db.connect().execute("UPDATE episodes SET subtitle_status = 'empty' WHERE project_id = ?",
                             (project["id"],))
        return None
    app = settings_store.get_settings()
    language = settings.get("language", "auto")
    lang = None if language == "auto" else language
    tpath = renderer.project_dir(project["id"]) / "transcript.json"
    pending = [e for e in episodes if e["subtitle_status"] in rebuild_states or force]
    if tpath.is_file() and not force:
        transcript = json.loads(tpath.read_text(encoding="utf-8"))
        if not pending:
            return transcript
    else:
        ctx.update(stage="Распознавание речи", progress=p0, message="Подготовка звука…", force=True)
        cache = subtitle_generator.transcript_cache_path(timeline["source_path"], app.whisper_model, lang)
        if cache.is_file() and not force:
            raw = json.loads(cache.read_text(encoding="utf-8"))
        else:
            wav = config.DIRS["temp"] / f"{project['id']}_speech.wav"
            try:
                extract_audio(timeline["source_path"], wav, timeline["duration"], ctx, p0, p0 + (p1 - p0) * 0.1)
                ctx.update(stage="Распознавание речи", message=f"Модель «{app.whisper_model}»: загрузка…",
                           force=True)
                started = time.monotonic()

                def on_prog(f: float) -> None:
                    el = time.monotonic() - started
                    eta = el / f * (1 - f) if f > 0.03 else None
                    ctx.update(progress=p0 + (p1 - p0) * (0.1 + 0.9 * f),
                               message=f"Распознано {int(f * 100)}% речи", eta=eta)

                raw = TRANSCRIBE(wav, model_size=app.whisper_model, language=lang, device=app.whisper_device,
                                 on_progress=on_prog, cancel_event=ctx.cancel_event, duration=timeline["duration"])
            finally:
                if not app.keep_temp_files:
                    downloader.remove_quietly(wav)
            subtitle_generator.save_json(cache, raw)
        # Shift from audio-stream time to the episode timeline (first video frame = 0).
        offset = float(timeline.get("audio_t0") or 0.0) - float(timeline.get("t0") or 0.0)
        words = [{**w, "start": round(w["start"] + offset, 3), "end": round(w["end"] + offset, 3)}
                 for w in raw.get("words", [])]
        transcript = {"language": raw.get("language"), "model": raw.get("model"), "words": words,
                      "segments": raw.get("segments", [])}
        skipped = raw.get("skipped") or []
        if skipped:
            spans = ", ".join(f"{fmt_span(x['start'])}–{fmt_span(x['end'])}" for x in skipped[:6])
            more = f" и ещё {len(skipped) - 6}" if len(skipped) > 6 else ""
            _add_warning(project["id"], f"Распознавание пропустило участки речи ({spans}{more}): "
                                        "субтитров там нет. Их можно добавить вручную в редакторе части.")
        subtitle_generator.save_json(tpath, transcript)
        db.update("projects", project["id"], {"transcript_path": str(tpath),
                                              "transcript_language": transcript.get("language")})
        pending = [e for e in episodes if e["subtitle_status"] in rebuild_states or force]

    _assign_cues(project, episodes, transcript, pending)
    return transcript


def _assign_cues(project: dict, episodes: list[dict], transcript: dict, pending: list[dict]) -> None:
    settings = project["settings"]
    words = transcript["words"]
    n = len(episodes)
    for e in pending:
        cues = subtitle_generator.cues_for_episode(words, e["start_time"], e["end_time"], e["idx"] == 0,
                                                   e["idx"] == n - 1, settings.get("subtitle_style"))
        db.update("episodes", e["id"], {"cues": cues, "subtitle_status": "auto" if cues else "empty",
                                        "status": "pending", "updated_at": db.now()})


def ensure_captions(project: dict, episodes: list[dict], transcript: dict | None, force: bool = False) -> None:
    app = settings_store.get_settings()
    settings = project["settings"]
    n = len(episodes)
    words = (transcript or {}).get("words", [])
    for e in episodes:
        if e.get("caption") and not force:
            continue
        text = " ".join(w["text"] for w in words if e["start_time"] <= w["start"] < e["end_time"])
        db.update("episodes", e["id"], {
            "caption": captions.make_caption(e["idx"] + 1, n, settings.get("caption_title", "")),
            "hashtags": captions.make_hashtags(app.default_hashtags, settings.get("hashtags", ""), text),
        })


def ensure_auto_crop(project: dict, timeline: dict, episodes: list[dict], ctx: JobContext,
                     p0: float, p1: float) -> None:
    todo = [e for e in episodes if renderer.auto_crop_needed(project, e)]
    for i, e in enumerate(todo):
        ctx.check()
        ctx.update(stage="Автокадрирование", progress=p0 + (p1 - p0) * i / max(1, len(todo)),
                   message=f"Поиск главного действия: часть {e['idx'] + 1} из {len(episodes)}")
        db.update("episodes", e["id"], {"auto_crop": renderer.compute_auto_crop(project, e, timeline)})


def project_gameplay_clips(project: dict) -> list[dict]:
    clips = []
    for mid in project.get("gameplay_media_ids") or []:
        m = db.get("media", mid)
        if m and m["status"] == "ready" and m.get("path") and Path(m["path"]).is_file():
            clips.append(m)
    return clips


def ensure_gameplay(project: dict, episodes: list[dict], clips: list[dict]) -> None:
    if not clips:
        raise UserError("Не выбрано ни одного видео геймплея. Добавьте геймплей в проект.")
    valid_ids = {c["id"] for c in clips}
    missing = [e for e in episodes if not (e.get("gameplay") or {}).get("media_id")
               or (e["gameplay"]["media_id"] not in valid_ids and not db.get("media", e["gameplay"]["media_id"]))]
    if not missing:
        return
    settings = project["settings"]
    specs = []
    for c in clips:
        us, ue = gameplay_manager.gameplay_range(c)
        specs.append({"id": c["id"], "usable_start": us, "usable_end": ue})
    choices = gameplay_manager.choose_gameplay(specs, [e["duration"] for e in episodes],
                                               settings.get("gameplay_mode", "continuous"),
                                               int(settings.get("seed") or 0))
    by_idx = {e["idx"]: c for e, c in zip(episodes, choices)}
    for e in missing:
        c = by_idx[e["idx"]]
        db.update("episodes", e["id"], {"gameplay": {"media_id": c.media_id, "offset": c.offset},
                                        "status": "pending"})


def _add_warning(project_id: str, text: str) -> None:
    p = db.get("projects", project_id)
    warnings = list(p.get("warnings") or [])
    if text not in warnings:
        warnings.append(text)
        db.update("projects", project_id, {"warnings": warnings})


def render_many(project: dict, episodes: list[dict], timeline: dict, ctx: JobContext,
                p0: float, p1: float) -> list[str]:
    """Render episodes (bounded concurrency). Returns error messages."""
    app = settings_store.get_settings()
    encoder = encoders.resolve_encoder(app.encoder)
    total = db.scalar("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (project["id"],))
    todo = sorted(episodes, key=lambda e: e["idx"])
    if not todo:
        return []
    weights = {e["id"]: max(0.1, e["duration"]) for e in todo}
    wsum = sum(weights.values())
    fractions = {e["id"]: 0.0 for e in todo}
    lock = threading.Lock()
    started = time.monotonic()
    errors: list[str] = []
    done_count = [0]

    def report(current: dict | None = None) -> None:
        with lock:
            done = sum(fractions[i] * weights[i] for i in fractions) / wsum
            el = time.monotonic() - started
        eta = el / done * (1 - done) if done > 0.02 else None
        msg = None
        if current is not None:
            msg = f"Создание части {current['idx'] + 1} из {total}"
        ctx.update(stage=msg or None, progress=p0 + (p1 - p0) * done, eta=eta,
                   message=f"Готово частей: {done_count[0]} из {len(todo)}")

    def work(e: dict) -> None:
        ctx.check()
        e = db.get("episodes", e["id"])
        gp_media = db.get("media", (e.get("gameplay") or {}).get("media_id") or "") if e.get("gameplay") else None
        db.update("episodes", e["id"], {"status": "rendering", "error": None, "updated_at": db.now()})
        report(e)

        def on_prog(f: float) -> None:
            with lock:
                fractions[e["id"]] = f
            report()

        try:
            fresh_project = db.get("projects", project["id"])
            result = renderer.render_episode(fresh_project, e, total, timeline, gp_media, encoder,
                                             on_progress=on_prog, cancel_event=ctx.cancel_event)
        except Cancelled:
            db.update("episodes", e["id"], {"status": "pending", "updated_at": db.now()})
            raise
        except UserError as exc:
            db.update("episodes", e["id"], {"status": "error", "error": exc.message, "updated_at": db.now()})
            with lock:
                errors.append(f"Часть {e['idx'] + 1}: {exc.message}")
                fractions[e["id"]] = 1.0
            log.warning("episode %s failed: %s %s", e["id"], exc.message, (exc.detail or "")[-800:])
            return
        # A freshly rendered video has not been reviewed yet: approval is never carried over.
        db.update("episodes", e["id"], {**result, "status": "ready", "error": None, "approved": 0,
                                        "rendered_at": db.now(), "updated_at": db.now()})
        with lock:
            fractions[e["id"]] = 1.0
            done_count[0] += 1
        report()

    workers = max(1, min(app.max_parallel_renders, len(todo)))
    if workers == 1:
        for e in todo:
            work(e)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
            futures = [pool.submit(work, e) for e in todo]
            try:
                for f in as_completed(futures):
                    f.result()
            except Cancelled:
                ctx.cancel_event.set()
                for f in futures:
                    f.cancel()
                raise
    return errors


def _finish_project(project_id: str) -> None:
    eps = db.query("episodes", "project_id = ?", (project_id,), "idx")
    status = "ready" if eps and all(e["status"] == "ready" for e in eps) else "error"
    db.update("projects", project_id, {"status": status, "updated_at": db.now(),
                                       "error": None if status == "ready" else "Не все части созданы"})


@register("build_series", "render", "Создание всех частей")
def job_build_series(ctx: JobContext) -> None:
    project = db.get("projects", ctx.job["project_id"])
    if not project:
        raise UserError("Проект не найден.")
    pid = project["id"]
    retranscribe = bool(ctx.payload.get("retranscribe"))
    rerender_all = bool(ctx.payload.get("rerender_all"))
    db.update("projects", pid, {"status": "processing", "error": None, "updated_at": db.now()})
    try:
        # 1. Import / validate videos
        ctx.update(stage="Загрузка видео", progress=0, message="Проверка исходных видео…", force=True)
        if not project.get("main_media_id"):
            raise UserError("В проекте не выбрано основное видео.")
        main = process_media(project["main_media_id"], ctx, 0, 8)
        gp_ids = project.get("gameplay_media_ids") or []
        for i, mid in enumerate(gp_ids):
            ctx.check()
            process_media(mid, ctx, 8 + 2 * i / max(1, len(gp_ids)), 8 + 2 * (i + 1) / max(1, len(gp_ids)))
        project = db.get("projects", pid)

        # 2. Analyse source / normalise timeline
        ctx.update(stage="Анализ исходника", progress=10, message="Анализ основного видео…", force=True)
        timeline = project.get("timeline")
        if not timeline or not Path(timeline.get("source_path", "")).is_file():
            timeline = prepare_timeline(project, main, ctx, 10, 18)
        project = db.get("projects", pid)

        # 3–4. Episode count and timeline
        episodes = ensure_episodes(project, timeline)
        ctx.update(stage="Анализ исходника", progress=18,
                   message=f"Длительность {video_splitter.format_timecode(timeline['duration'])}, "
                           f"частей: {len(episodes)}", force=True)
        ctx.check()

        # 5. Speech recognition (once for the whole video)
        transcript = ensure_transcript(project, timeline, episodes, ctx, 18, 45, force=retranscribe,
                                       rebuild=bool(ctx.payload.get("rebuild_subtitles")))
        episodes = db.query("episodes", "project_id = ?", (pid,), "idx")
        ensure_captions(project, episodes, transcript)
        ctx.check()

        # 5b. Automatic framing of the cartoon for every part
        ensure_auto_crop(db.get("projects", pid), timeline, episodes, ctx, 45, 47)
        episodes = db.query("episodes", "project_id = ?", (pid,), "idx")

        # 6. Gameplay
        ctx.update(stage="Подготовка геймплея", progress=45, message="Подбор фрагментов геймплея…", force=True)
        ensure_gameplay(project, episodes, project_gameplay_clips(project))
        episodes = db.query("episodes", "project_id = ?", (pid,), "idx")

        # 7. Subtitle files (editable .ass/.srt for every episode)
        ctx.update(stage="Добавление субтитров", progress=47, message="Создание файлов субтитров…", force=True)
        project = db.get("projects", pid)
        for e in episodes:
            renderer.write_subtitle_files(project, e, len(episodes))

        # 8–10. Render every episode that is not ready yet (resumes after restart)
        todo = [e for e in episodes if rerender_all or e["status"] != "ready"
                or not e.get("video_path") or not Path(e["video_path"]).is_file()]
        errors = render_many(project, todo, timeline, ctx, 48, 97)

        # 11. Verify outputs
        ctx.update(stage="Проверка результата", progress=98, message="Проверка файлов…", force=True)
        episodes = db.query("episodes", "project_id = ?", (pid,), "idx")
        missing = [e for e in episodes if e["status"] == "ready" and not Path(e["video_path"] or "").is_file()]
        for e in missing:
            db.update("episodes", e["id"], {"status": "error", "error": "Файл части не найден"})
            errors.append(f"Часть {e['idx'] + 1}: файл не найден")
        frames = sum(e["end_frame"] - e["start_frame"] for e in episodes)
        if frames != timeline["total_frames"]:
            raise UserError("Внутренняя ошибка: части не покрывают видео целиком.")
        _finish_project(pid)
        if errors:
            raise UserError("Не все части созданы. " + " ".join(errors[:5]))
        ctx.update(stage="Готово", progress=100, message=f"Создано частей: {len(episodes)}", force=True)
    except Cancelled:
        db.update("projects", pid, {"status": "cancelled", "updated_at": db.now()})
        raise
    except UserError as exc:
        db.update("projects", pid, {"status": "error", "error": exc.message, "updated_at": db.now()})
        raise


@register("render_episodes", "render", "Пересоздание частей")
def job_render_episodes(ctx: JobContext) -> None:
    project = db.get("projects", ctx.job["project_id"])
    if not project:
        raise UserError("Проект не найден.")
    timeline = project.get("timeline")
    if not timeline or not Path(timeline.get("source_path", "")).is_file():
        raise UserError("Исходное видео проекта не найдено. Запустите «Создать все части».")
    ids = ctx.payload.get("episode_ids") or []
    episodes = [e for e in (db.get("episodes", i) for i in ids) if e and e["project_id"] == project["id"]]
    if not episodes:
        raise UserError("Нет частей для пересоздания.")
    ensure_gameplay(project, db.query("episodes", "project_id = ?", (project["id"],), "idx"),
                    project_gameplay_clips(project))
    episodes = [db.get("episodes", e["id"]) for e in episodes]
    db.update("projects", project["id"], {"status": "processing", "updated_at": db.now()})
    n = len(episodes)
    ctx.update(stage="Добавление субтитров", progress=2, message="Обновление субтитров…", force=True)
    errors = render_many(project, episodes, timeline, ctx, 5, 97)
    ctx.update(stage="Проверка результата", progress=98, force=True)
    _finish_project(project["id"])
    if errors:
        raise UserError(" ".join(errors[:5]))
    ctx.update(stage="Готово", progress=100, message=f"Пересоздано частей: {n}", force=True)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def tiktok_client() -> publisher.TikTokClient:
    app = settings_store.get_settings()
    return publisher.get_client(app.tiktok_client_key, app.tiktok_redirect_uri)


@register("tiktok_publish", "io", "Публикация в TikTok")
def job_tiktok_publish(ctx: JobContext) -> None:
    ep = db.get("episodes", ctx.job["episode_id"])
    if not ep:
        raise UserError("Часть не найдена.")
    if not ep["approved"]:
        raise UserError("Часть не одобрена. Публикация возможна только после одобрения.")
    if ep["status"] != "ready" or not ep.get("video_path") or not Path(ep["video_path"]).is_file():
        raise UserError("Видео части ещё не готово.")
    client = tiktok_client()
    if not client.is_configured() or not client.tokens():
        raise UserError("Аккаунт TikTok не подключён. Используйте ручную публикацию или подключите API в настройках.")
    mode = ctx.payload.get("mode", "direct")
    caption = (ep.get("caption") or "").strip()
    if ep.get("hashtags"):
        caption = f"{caption} {ep['hashtags']}".strip()
    info = dict(ep.get("publish_info") or {})
    db.update("episodes", ep["id"], {"publish_status": "publishing", "updated_at": db.now()})
    try:
        ctx.update(stage="Загрузка в TikTok", progress=5, message="Подготовка публикации…", force=True)
        publish_id = client.publish_video(
            Path(ep["video_path"]), caption, mode=mode,
            privacy_level=ctx.payload.get("privacy_level", "SELF_ONLY"),
            disable_comment=bool(ctx.payload.get("disable_comment")),
            disable_duet=bool(ctx.payload.get("disable_duet")),
            disable_stitch=bool(ctx.payload.get("disable_stitch")),
            duration=float(ep["duration"]),
            on_progress=lambda f, m: ctx.update(progress=5 + 70 * f, message=m),
            cancel_event=ctx.cancel_event,
        )
        info.update({"publish_id": publish_id, "mode": mode, "started_at": db.now(), "error": None})
        db.update("episodes", ep["id"], {"publish_info": info})
        ctx.update(stage="Проверка публикации", progress=80, message="Ожидание подтверждения TikTok…", force=True)
        deadline = time.monotonic() + 15 * 60
        status, msg, final = "publishing", "Ожидание ответа TikTok", False
        while time.monotonic() < deadline:
            ctx.check()
            data = client.fetch_status(publish_id, ctx.cancel_event)
            status, msg, final = publisher.interpret_status(data, mode)
            info.update({"status_raw": data.get("status"), "message": msg,
                         "post_ids": data.get("publicaly_available_post_id") or data.get("publicly_available_post_id")})
            if final:
                break
            ctx.update(message=msg)
            for _ in range(10):
                ctx.check()
                time.sleep(0.5)
        info["checked_at"] = db.now()
        db.update("episodes", ep["id"], {"publish_status": status, "publish_info": info, "updated_at": db.now()})
        if status == "failed":
            raise UserError(msg)
        if not final:
            ctx.update(stage="Готово", progress=100,
                       message="TikTok ещё обрабатывает видео. Статус можно обновить позже.", force=True)
    except UserError as exc:
        info["error"] = exc.message
        db.update("episodes", ep["id"], {"publish_status": "failed", "publish_info": info, "updated_at": db.now()})
        raise
    except Cancelled:
        db.update("episodes", ep["id"], {"publish_status": "not_published", "updated_at": db.now()})
        raise
