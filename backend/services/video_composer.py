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

LAYOUTS = {"40/60": 0.40, "50/50": 0.50, "55/45": 0.55, "60/40": 0.60, "65/35": 0.65, "70/30": 0.70}
MAX_OUTPUT_FPS = 60
DIVIDER_PX = 6
BANNER_MIN, BANNER_MAX = 60, 700


def layout_heights(layout: str, height: int = config.OUTPUT_HEIGHT) -> tuple[int, int]:
    ratio = LAYOUTS.get(layout, 0.5)
    top = int(round(height * ratio / 2)) * 2
    return top, height - top


def banner_height(banner: dict | None) -> int:
    if not banner or not banner.get("enabled"):
        return 0
    h = int(banner.get("height") or 240)
    return max(BANNER_MIN, min(BANNER_MAX, h)) // 2 * 2


def screen_regions(layout: str, banner: dict | None = None, gameplay_top: bool = False,
                   height: int = config.OUTPUT_HEIGHT) -> dict:
    """Vertical layout of the frame: {"main": (y, h), "game": (y, h), "banner": (y, h) | None}.

    The layout ratio is the share of the cartoon. A banner in "reserve" mode takes its own strip
    (top of the screen, between the two pictures or at the bottom) and the pictures share the rest;
    in "overlay" mode it is drawn over the pictures and does not change them.
    """
    bh = banner_height(banner)
    reserve = bh > 0 and (banner or {}).get("mode", "reserve") == "reserve"
    pos = (banner or {}).get("position", "top")
    avail = height - (bh if reserve else 0)
    main_h, game_h = layout_heights(layout, avail)
    first, second = (("game", game_h), ("main", main_h)) if gameplay_top else (("main", main_h), ("game", game_h))
    order = [first, second]
    if reserve:
        order.insert({"top": 0, "middle": 1, "bottom": 2}.get(pos, 0), ("banner", bh))
    out: dict = {"banner": None}
    y = 0
    for name, h in order:
        out[name] = (y, h)
        y += h
    if bh and not reserve:
        out["banner"] = ({"top": 0, "middle": None, "bottom": height - bh}.get(pos, 0), bh)
        if out["banner"][0] is None:  # centred on the split between the pictures
            split = out[second[0]][0]
            out["banner"] = (max(0, min(height - bh, split - bh // 2)) // 2 * 2, bh)
    return out


def split_y(regions: dict) -> int:
    """The edge between the cartoon and the gameplay (subtitles sit here by default)."""
    my, mh = regions["main"]
    gy, _ = regions["game"]
    return my + mh if my < gy else my


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
    divider: bool = False  # thin black line on the split
    main_box: dict | None = None  # content area without black bars {x, y, w, h}
    game_box: dict | None = None
    audio_delay: float = 0.0  # seconds of silence before main audio (stream start offset)
    # Virtual camera for the cartoon: [{"f0", "f1", "zoom", "keys": [[frame, x, y], ...]}] covering the part
    # (frames on the output grid). None = one fixed crop position from `crop`.
    camera: list[dict] | None = None
    gameplay_top: bool = False
    banner: dict | None = None  # project banner settings; "image_relpath" set when a picture is used
    width: int = config.OUTPUT_WIDTH
    height: int = config.OUTPUT_HEIGHT


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _cover(box: dict | None, w: int, h: int, zoom: float) -> list[str]:
    parts = []
    if box:
        parts.append(f"crop={int(box['w'])}:{int(box['h'])}:{int(box['x'])}:{int(box['y'])}")
    z = max(1.0, min(3.0, float(zoom or 1.0)))
    parts.append(f"scale={_even(w * z)}:{_even(h * z)}:force_original_aspect_ratio=increase:flags=bicubic")
    return parts


def fit_chain(box: dict | None, w: int, h: int, zoom: float, fx: float, fy: float) -> str:
    """Remove black bars, cover-scale (optionally zoomed) without distortion, crop to w×h at (fx, fy)."""
    return ",".join(_cover(box, w, h, zoom) + [f"crop={w}:{h}:(iw-ow)*{fx:.4f}:(ih-oh)*{fy:.4f}"])


def path_expr(keys: list[tuple[int, float]]) -> str:
    """ffmpeg expression of frame number n: holds between keys, eases (cosine) from one key to the next.

    keys are (frame, value) sorted by frame; a pair one frame apart is a hard cut.
    """
    keys = [(int(f), round(float(v), 4)) for f, v in keys]
    if not keys:
        return "0.5"
    expr = f"{keys[-1][1]:g}"
    for (f0, v0), (f1, v1) in zip(reversed(keys[:-1]), reversed(keys[1:])):
        if v0 == v1:
            seg = f"{v0:g}"
        else:
            span = max(1, f1 - f0)
            seg = f"{v0:g}+({v1 - v0:.4f})*(1-cos(PI*clip((n-{f0})/{span},0,1)))/2"
        expr = f"if(lt(n,{f1}),{seg},{expr})"
    return expr


def camera_chain(box: dict | None, w: int, h: int, zoom: float, keys: list) -> str:
    """Cover-scale + a crop window that moves along the camera keys [[frame, x, y], ...]."""
    xs = path_expr([(k[0], k[1]) for k in keys])
    ys = path_expr([(k[0], k[2]) for k in keys])
    crop = f"crop=w={w}:h={h}:x='(iw-ow)*({xs})':y='(ih-oh)*({ys})'"
    return ",".join(_cover(box, w, h, zoom) + [crop])


def camera_runs(camera: list[dict]) -> list[dict]:
    """Merge consecutive shots with the same zoom: one scaled branch per run, keys relative to the run."""
    runs: list[dict] = []
    for shot in camera:
        z = round(float(shot.get("zoom") or 1.0), 3)
        if runs and runs[-1]["zoom"] == z and runs[-1]["f1"] == shot["f0"]:
            run = runs[-1]
            run["f1"] = shot["f1"]
        else:
            run = {"f0": int(shot["f0"]), "f1": int(shot["f1"]), "zoom": z, "keys": []}
            runs.append(run)
        keys = [list(k) for k in shot["keys"]] or [[shot["f0"], 0.5, 0.5]]
        if keys[0][0] > shot["f0"]:
            keys.insert(0, [shot["f0"], keys[0][1], keys[0][2]])
        if run["keys"]:
            # Hard cut: hold the previous shot's last position up to the frame before the new shot.
            run["keys"].append([shot["f0"] - 1, run["keys"][-1][1], run["keys"][-1][2]])
        run["keys"].extend(keys)
    for run in runs:
        dedup: list[list] = []
        for k in run["keys"]:
            k = [int(k[0]) - run["f0"], float(k[1]), float(k[2])]
            if dedup and k[0] <= dedup[-1][0]:
                dedup[-1] = k
            else:
                dedup.append(k)
        run["keys"] = dedup
    return runs


def main_chains(spec: EpisodeRenderSpec, src: str, w: int, h: int, n_frames: int, out_fps: Fraction) -> list[str]:
    """The cartoon picture (src label → [top]): fixed crop, or the virtual camera (pans, cuts, zoom changes)."""
    cx = _clamp01(spec.crop.get("main_x"))
    cy = _clamp01(spec.crop.get("main_y"))
    if not spec.camera:
        return [f"{src}" + fit_chain(spec.main_box, w, h, spec.crop.get("main_zoom", 1.0), cx, cy) +
                ",setsar=1,format=yuv420p[top]"]
    runs = camera_runs(spec.camera)
    if len(runs) == 1:
        return [f"{src}" + camera_chain(spec.main_box, w, h, runs[0]["zoom"], runs[0]["keys"]) +
                ",setsar=1,format=yuv420p[top]"]
    # Different zoom levels: split the picture into frame-exact runs, scale each, join them back.
    chains = [f"{src}split={len(runs)}" + "".join(f"[cam{i}]" for i in range(len(runs)))]
    labels = []
    for i, run in enumerate(runs):
        end = n_frames if i == len(runs) - 1 else run["f1"]
        chains.append(
            f"[cam{i}]trim=start_frame={run['f0']}:end_frame={end},setpts=PTS-STARTPTS,"
            + camera_chain(spec.main_box, w, h, run["zoom"], run["keys"])
            + f",setsar=1,format=yuv420p[camv{i}]")
        labels.append(f"[camv{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(runs)}:v=1:a=0,"
                  f"settb={out_fps.denominator}/{out_fps.numerator},setpts=N[top]")
    return chains


def main_trim_chain(win: CutWindow, fps: Fraction) -> str:
    """Frames of the part on an exact constant-rate grid (shared with the camera analysis)."""
    chain = (f"trim=start={fmt_time(win.video_trim_start)}:end={fmt_time(win.video_trim_end)},"
             f"settb={fps.denominator}/{fps.numerator},setpts=N")
    out_fps = output_fps(fps)
    if out_fps != fps:
        chain += f",fps={out_fps.numerator}/{out_fps.denominator}"
    return chain


def _ass_filter(spec: EpisodeRenderSpec) -> str:
    # Paths are relative and contain only safe characters (validated ids), so no escaping issues
    # with drive letters or spaces on Windows.
    f = f"ass=filename={spec.ass_relpath}"
    if spec.fontsdir_relpath:
        f += f":fontsdir={spec.fontsdir_relpath}"
    return f


def _hex(color: str | None, default: str = "000000") -> str:
    c = (color or "").lstrip("#")
    return c if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c) else default


def build_filter_and_inputs(spec: EpisodeRenderSpec) -> tuple[list[str], str, str, str]:
    """Return (input_args, filter_complex, video_label, audio_label)."""
    w, h = spec.width, spec.height
    regions = screen_regions(spec.layout, spec.banner, spec.gameplay_top, h)
    top_h = regions["main"][1]
    bot_h = regions["game"][1]
    win = spec.window
    out_fps = output_fps(spec.fps)
    fps_str = f"{out_fps.numerator}/{out_fps.denominator}"
    dur = win.duration
    n_frames = max(1, int(round(dur * out_fps)))
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
    chains.append("[0:v:0]" + main_trim_chain(win, spec.fps) + "[mainsrc]")
    chains += main_chains(spec, "[mainsrc]", w, top_h, n_frames, out_fps)

    use_game_audio = spec.gameplay_audio and spec.gameplay_has_audio
    if spec.gameplay_path and spec.gameplay_pieces:
        piece_labels = []
        for n, piece in enumerate(spec.gameplay_pieces):
            inputs += ["-ss", fmt_time(piece.start), "-t", fmt_time(piece.duration), "-i", spec.gameplay_path]
            chains.append(
                f"[{idx}:v:0]fps={fps_str},"
                + fit_chain(spec.game_box, w, bot_h, spec.crop.get("game_zoom", 1.0), gx, gy)
                + f",setsar=1,format=yuv420p[gv{n}]"
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

    # Banner picture (scaled to fit its strip, never stretched).
    banner = spec.banner or {}
    breg = regions.get("banner")
    bimg = None
    if breg and banner.get("image_relpath"):
        pad = int(banner.get("padding", 0) or 0)
        bw, bh = w - 2 * pad, breg[1] - 2 * pad
        inputs += ["-i", banner["image_relpath"]]
        chains.append(f"[{idx}:v:0]scale={_even(bw)}:{_even(bh)}:force_original_aspect_ratio=decrease:"
                      f"flags=lanczos,format=rgba[bimg]")
        bimg = "[bimg]"
        idx += 1

    order = sorted(((y, name) for name, (y, _) in
                    [("main", regions["main"]), ("game", regions["game"])] +
                    ([("banner", breg)] if breg and banner.get("mode", "reserve") == "reserve" else [])))
    labels = {"main": "[top]", "game": "[bot]"}
    if "banner" in [n for _, n in order]:
        chains.append(f"color=c=0x{_hex(banner.get('background'))}:s={w}x{breg[1]}:r={fps_str}:d={fmt_time(dur + 1)},"
                      f"trim=end_frame={n_frames},format=yuv420p[bstrip0]")
        if bimg:
            chains.append(f"[bstrip0]{bimg}overlay=x=(W-w)/2:y=(H-h)/2:format=auto,format=yuv420p[bstrip]")
        else:
            chains.append("[bstrip0]null[bstrip]")
        labels["banner"] = "[bstrip]"
    stack_in = "".join(labels[name] for _, name in order)
    chains.append(f"{stack_in}vstack=inputs={len(order)}:shortest=1[stack0]")
    cur = "[stack0]"
    if spec.divider:
        lw = DIVIDER_PX
        (my, mh), (gy, gh) = regions["main"], regions["game"]
        if my + mh == gy or gy + gh == my:  # the pictures touch (no banner strip between them)
            sy = split_y(regions)
            chains.append(f"{cur}drawbox=x=0:y={max(0, sy - lw // 2)}:w=iw:h={lw}:color=black:t=fill[stackd]")
            cur = "[stackd]"
    if breg and bimg and banner.get("mode", "reserve") == "overlay":
        chains.append(f"{cur}{bimg}overlay=x=(W-w)/2:y={breg[0]}+({breg[1]}-h)/2:format=auto,format=yuv420p[stackb]")
        cur = "[stackb]"
    chains.append(f"{cur}null[stack]")
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
