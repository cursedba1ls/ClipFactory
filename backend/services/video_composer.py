"""Builds the ffmpeg command for one vertical split-screen episode.

Top: main video segment (priority, original audio). Bottom: gameplay (muted by
default, looped from pieces). Subtitles and the episode label are burned in
from an ASS file in the same encoding pass — a single re-encode per episode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from backend import config
from backend.services.ffmpeg_utils import fmt_time
from backend.services.gameplay_manager import GameplayPiece
from backend.services.video_splitter import CutWindow

LAYOUTS = {"50/50": 0.50, "55/45": 0.55, "60/40": 0.60}
MAX_OUTPUT_FPS = 60


def layout_heights(layout: str, height: int = config.OUTPUT_HEIGHT) -> tuple[int, int]:
    ratio = LAYOUTS.get(layout, 0.5)
    top = int(round(height * ratio / 2)) * 2
    return top, height - top


def output_fps(fps: Fraction) -> Fraction:
    return fps if fps <= MAX_OUTPUT_FPS else Fraction(MAX_OUTPUT_FPS)


def _clamp01(v, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


@dataclass
class EpisodeRenderSpec:
    main_path: str
    window: CutWindow
    fps: Fraction
    main_has_audio: bool
    gameplay_path: str | None
    gameplay_pieces: list[GameplayPiece]
    gameplay_has_audio: bool = False
    gameplay_audio: bool = False
    gameplay_volume: float = 0.15
    layout: str = "50/50"
    crop: dict = field(default_factory=dict)
    ass_relpath: str | None = None  # relative to the ffmpeg working directory
    fontsdir_relpath: str | None = None
    audio_delay: float = 0.0  # seconds of silence before main audio (stream start offset)
    width: int = config.OUTPUT_WIDTH
    height: int = config.OUTPUT_HEIGHT


def _ass_filter(spec: EpisodeRenderSpec) -> str:
    # Paths are relative and contain only safe characters (validated ids), so no escaping issues
    # with drive letters or spaces on Windows.
    f = f"ass=filename={spec.ass_relpath}"
    if spec.fontsdir_relpath:
        f += f":fontsdir={spec.fontsdir_relpath}"
    return f


def build_filter_and_inputs(spec: EpisodeRenderSpec) -> tuple[list[str], str, str, str]:
    """Return (input_args, filter_complex, video_label, audio_label)."""
    w, h = spec.width, spec.height
    top_h, bot_h = layout_heights(spec.layout, h)
    win = spec.window
    out_fps = output_fps(spec.fps)
    fps_str = f"{out_fps.numerator}/{out_fps.denominator}"
    dur = win.duration
    n_frames = max(1, int(round(dur * out_fps)))
    cx = _clamp01(spec.crop.get("main_x"))
    cy = _clamp01(spec.crop.get("main_y"))
    gx = _clamp01(spec.crop.get("game_x"))
    gy = _clamp01(spec.crop.get("game_y"))

    inputs: list[str] = []
    if win.seek > 0:
        inputs += ["-ss", fmt_time(win.seek)]
    inputs += ["-i", spec.main_path]
    idx = 1
    chains: list[str] = []

    # After trimming, re-time the selected frames onto an exact constant-rate grid (frame n at n/fps).
    # Containers with coarse time bases (MKV/WebM use 1 ms) carry rounded timestamps; without this
    # the top and bottom streams would not line up and vstack would emit extra frames.
    top = (
        f"[0:v:0]trim=start={fmt_time(win.video_trim_start)}:end={fmt_time(win.video_trim_end)},"
        f"settb={spec.fps.denominator}/{spec.fps.numerator},setpts=N"
    )
    if out_fps != spec.fps:
        top += f",fps={fps_str}"
    top += (
        f",scale={w}:{top_h}:force_original_aspect_ratio=increase:flags=bicubic,"
        f"crop={w}:{top_h}:(iw-ow)*{cx:.4f}:(ih-oh)*{cy:.4f},setsar=1,format=yuv420p[top]"
    )
    chains.append(top)

    use_game_audio = spec.gameplay_audio and spec.gameplay_has_audio
    if spec.gameplay_path and spec.gameplay_pieces:
        piece_labels = []
        for n, piece in enumerate(spec.gameplay_pieces):
            inputs += ["-ss", fmt_time(piece.start), "-t", fmt_time(piece.duration), "-i", spec.gameplay_path]
            chains.append(
                f"[{idx}:v:0]fps={fps_str},scale={w}:{bot_h}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={w}:{bot_h}:(iw-ow)*{gx:.4f}:(ih-oh)*{gy:.4f},setsar=1,format=yuv420p[gv{n}]"
            )
            piece_labels.append(f"[gv{n}]")
            if use_game_audio:
                chains.append(
                    f"[{idx}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[ga{n}]"
                )
                piece_labels.append(f"[ga{n}]")
            idx += 1
        n_p = len(spec.gameplay_pieces)
        if use_game_audio:
            chains.append(f"{''.join(piece_labels)}concat=n={n_p}:v=1:a=1[gcat][gacat]")
            chains.append(
                f"[gacat]atrim=duration={fmt_time(dur)},asetpts=PTS-STARTPTS,"
                f"volume={max(0.0, min(2.0, spec.gameplay_volume)):.3f}[gaud]"
            )
        else:
            chains.append(f"{''.join(piece_labels)}concat=n={n_p}:v=1:a=0[gcat]")
        # Same exact frame grid as the top stream: concatenated pieces otherwise start off-grid.
        chains.append(f"[gcat]settb={out_fps.denominator}/{out_fps.numerator},setpts=N,"
                      f"trim=end_frame={n_frames}[bot]")
    else:
        chains.append(f"color=c=black:s={w}x{bot_h}:r={fps_str}:d={fmt_time(dur + 1)},"
                      f"trim=end_frame={n_frames},format=yuv420p[bot]")

    chains.append("[top][bot]vstack=inputs=2:shortest=1[stack]")
    if spec.ass_relpath:
        chains.append(f"[stack]{_ass_filter(spec)}[vout]")
    else:
        chains.append("[stack]null[vout]")

    # Main audio: exact window aligned with the video frames, padded to the episode length.
    if spec.main_has_audio:
        a = (
            f"[0:a:0]atrim=start={fmt_time(win.audio_start)}:end={fmt_time(win.audio_end)},"
            f"asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        )
        if spec.audio_delay > 0.001:
            ms = int(round(spec.audio_delay * 1000))
            a += f",adelay=delays={ms}:all=1"
        a += f",apad=whole_dur={fmt_time(dur)},atrim=duration={fmt_time(dur)}[maud]"
        chains.append(a)
    else:
        chains.append(
            f"anullsrc=r=48000:cl=stereo,atrim=duration={fmt_time(dur)},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[maud]"
        )

    if use_game_audio and spec.gameplay_path and spec.gameplay_pieces:
        chains.append("[maud][gaud]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        a_label = "[aout]"
    else:
        a_label = "[maud]"
    return inputs, ";".join(chains), "[vout]", a_label


def build_command(spec: EpisodeRenderSpec, encoder_args: list[str], audio_args: list[str],
                  output: str) -> list[str]:
    inputs, graph, v, a = build_filter_and_inputs(spec)
    return [
        *inputs,
        "-filter_complex", graph,
        "-map", v, "-map", a,
        *encoder_args,
        *audio_args,
        "-movflags", "+faststart",
        "-metadata", "comment=ClipFactory",
        output,
    ]
