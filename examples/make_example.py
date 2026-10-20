"""Generates ClipFactory example sources (original content, free to use):

  zakatun.mp4   — 3-minute cartoon "Закатун" (a little creature watching the sunset with an owl friend).
                  Widescreen 2.35:1 picture with black bars inside a 16:9 frame, characters on the left /
                  right / centre in different minutes — a realistic test for automatic cropping.
                  No voice: the soundtrack is soft music only.
  zakatun.srt   — Russian subtitles for the cartoon (attach in «Новый проект» → «Субтитры из файла»).
  obby_gameplay.mp4 — 40-second Roblox-style obby run (blocky runner on checkered platforms), loops seamlessly.

Usage:  python examples/make_example.py [output_dir]      (needs FFmpeg, Pillow, numpy)
"""
from __future__ import annotations

import functools
import math
import multiprocessing as mp
import random
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

FPS = 24
DURATION = 180.0
FRAME_W, FRAME_H = 1920, 1080
PIC_W, PIC_H = 1920, 816          # 2.35:1 picture
BAR = (FRAME_H - PIC_H) // 2       # black bars top/bottom
HORIZON = int(PIC_H * 0.66)

# (start, end, speaker, text) — speaker "z" = Закатун, "o" = Совёнок
DIALOGUE = [
    (2.0, 4.6, "z", "Привет! Меня зовут Закатун."),
    (5.5, 8.6, "z", "Каждый вечер я прихожу сюда смотреть на закат."),
    (10.0, 13.0, "z", "Сегодня солнце какое-то особенно огромное!"),
    (15.0, 18.0, "z", "Интересно, куда оно прячется каждую ночь?"),
    (20.0, 23.5, "z", "Может, оно ныряет прямо в море?"),
    (26.0, 29.0, "z", "Надо проверить! Пойду поближе."),
    (33.0, 36.0, "z", "Ого, вода тёплая, как чай!"),
    (39.0, 42.0, "z", "Солнце, подожди меня, не уходи!"),
    (46.0, 49.0, "z", "Эх, опять оно убегает от меня."),
    (52.0, 55.5, "z", "Ладно, посижу тут и подумаю."),
    (56.5, 59.5, "z", "Ой, кто это там летит?"),
    (62.0, 65.0, "o", "Привет, Закатун! Ты чего грустишь?"),
    (66.0, 69.0, "z", "Я хочу узнать, куда уходит солнце."),
    (71.0, 74.0, "o", "Это очень просто! Оно идёт спать за горы."),
    (76.0, 79.0, "z", "За горы? Но там же темно и страшно!"),
    (81.0, 84.0, "o", "Не бойся, я покажу тебе дорогу."),
    (87.0, 90.0, "z", "Правда? Тогда полетели! То есть пошли!"),
    (93.0, 96.0, "o", "Смотри, с вершины холма видно лучше всего."),
    (99.0, 102.0, "z", "Ух ты, отсюда видно всё море!"),
    (105.0, 108.0, "z", "А небо стало розовым, как клубничное мороженое."),
    (111.0, 114.0, "o", "Тихо. Сейчас будет самое красивое."),
    (116.0, 119.0, "z", "Что? Что будет?"),
    (121.0, 124.0, "o", "Солнце касается воды!"),
    (126.0, 129.0, "o", "Смотри, оно рисует золотую дорожку."),
    (131.0, 134.0, "z", "А по ней можно дойти до самого солнца?"),
    (136.0, 139.0, "o", "Только во сне, Закатун. Только во сне."),
    (142.0, 145.0, "z", "Тогда сегодня я лягу спать пораньше!"),
    (148.0, 151.0, "o", "А завтра расскажешь, что тебе приснилось?"),
    (154.0, 157.0, "z", "Обязательно! Спасибо тебе, Совёнок."),
    (160.0, 163.0, "z", "Спокойной ночи, солнце! До завтра!"),
    (167.0, 171.0, "z", "Подписывайся, чтобы узнать, что приснится Закатуну!"),
    (174.0, 177.0, "z", "Конец."),
]


def lerp(a, b, t):
    return a + (b - a) * t


def smooth(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def keyframes(t, keys):
    """Piecewise smooth interpolation over [(time, value), ...]."""
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t <= t1:
            k = smooth((t - t0) / (t1 - t0))
            if isinstance(v0, tuple):
                return tuple(lerp(a, b, k) for a, b in zip(v0, v1))
            return lerp(v0, v1, k)
    return keys[-1][1]


def speaking(t, who):
    for s, e, w, _ in DIALOGUE:
        if w == who and s <= t < e:
            return True
    return False


# ---------------------------------------------------------------------------
# Cartoon
# ---------------------------------------------------------------------------

SKY_TOP = [(0, (70, 120, 200)), (60, (90, 70, 160)), (120, (60, 40, 110)), (180, (25, 25, 70))]
SKY_HOR = [(0, (255, 200, 120)), (60, (255, 150, 90)), (120, (255, 110, 110)), (180, (200, 90, 120))]
SUN_Y = [(0, 0.22), (60, 0.36), (120, 0.56), (150, 0.66), (180, 0.80)]

# Character positions as a fraction of the picture width (left in minute 1, right in minute 2, centre in 3).
Z_X = [(0, -0.08), (7, 0.27), (26, 0.27), (32, 0.33), (60, 0.33), (66, 0.66), (87, 0.66), (97, 0.5), (180, 0.5)]
Z_Y = [(0, 0.80), (87, 0.80), (97, 0.665), (180, 0.665)]
O_X = [(0, 1.15), (59, 1.15), (62, 0.84), (87, 0.84), (97, 0.58), (180, 0.58)]
O_Y = [(0, 0.25), (59, 0.25), (62, 0.42), (87, 0.42), (97, 0.52), (180, 0.52)]


def supersampled(size, draw_fn, scale=3):
    w, h = size
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(img), scale)
    return img.resize((w, h), Image.LANCZOS)


def make_hills():
    def fn(d, s):
        rnd = random.Random(3)
        # distant mountains end at the horizon, so the sea with the sun path stays visible
        for layer, (col, base, amp) in enumerate([((90, 60, 110), 0.56, 0.10), ((65, 45, 90), 0.62, 0.05)]):
            pts = [(0, HORIZON * s)]
            for i in range(0, 41):
                x = PIC_W * i / 40
                y = PIC_H * (base - amp * (0.6 * math.sin(i * 0.5 + layer) + 0.4 * rnd.random()))
                pts.append((x * s, min(y, HORIZON) * s))
            pts.append((PIC_W * s, HORIZON * s))
            d.polygon(pts, fill=col + (255,))
        # sandy beach in the foreground
        beach = [(0, PIC_H * s)] + [(PIC_W * i / 30 * s, PIC_H * (0.86 + 0.015 * math.sin(i * 0.9)) * s)
                                     for i in range(31)] + [(PIC_W * s, PIC_H * s)]
        d.polygon(beach, fill=(235, 190, 130, 255))
        # the grassy hill in the centre where the friends end up
        d.ellipse([(PIC_W * 0.32) * s, (PIC_H * 0.70) * s, (PIC_W * 0.68) * s, (PIC_H * 1.20) * s],
                  fill=(70, 125, 75, 255))
    return supersampled((PIC_W, PIC_H), fn, 2)


def make_cloud(w, h, seed):
    rnd = random.Random(seed)

    def fn(d, s):
        for _ in range(7):
            cx, cy = rnd.uniform(0.32, 0.68) * w, rnd.uniform(0.45, 0.6) * h
            r = rnd.uniform(0.12, 0.2) * w
            d.ellipse([(cx - r) * s, (cy - r * 0.6) * s, (cx + r) * s, (cy + r * 0.6) * s], fill=(255, 255, 255, 200))
    return supersampled((w, h), fn).filter(ImageFilter.GaussianBlur(2))


def zakatun_sprite(t, walking, talk, blink):
    """Quantise the animation state so identical poses are drawn once."""
    phase = round((t * 9) % (2 * math.pi) / (2 * math.pi) * 12) % 12 if walking else round((t * 2) % (2 * math.pi) / (2 * math.pi) * 8) % 8
    tail = round((t * 3) % (2 * math.pi) / (2 * math.pi) * 8) % 8
    mouth = round(abs(math.sin(t * 14)) * 3) if talk else -1
    return _zakatun(walking, phase, tail, mouth, blink)


@functools.lru_cache(maxsize=512)
def _zakatun(walking, phase, tail_i, mouth, blink):
    t_walk = phase / 12 * 2 * math.pi / 9
    t = t_walk if walking else phase / 8 * 2 * math.pi / 2
    tail_t = tail_i / 8 * 2 * math.pi / 3
    return draw_zakatun(t, walking, mouth >= 0, blink, tail_t, max(0, mouth) / 3)


def draw_zakatun(t, walking, talk, blink, tail_t=0.0, mouth_k=0.5):
    """The hero: a round orange fluffball with big eyes, ears and a striped tail."""
    W, H = 300, 320

    def fn(d, s):
        bob = math.sin(t * 9) * 6 if walking else math.sin(t * 2) * 2
        cx, cy = 150, 190 + bob
        step = math.sin(t * 9) * 14 if walking else 0
        # legs
        for dx, ph in ((-40, step), (40, -step)):
            d.rounded_rectangle([(cx + dx - 16 + ph * 0.3) * s, (cy + 70) * s, (cx + dx + 16 + ph * 0.3) * s,
                                 (cy + 115) * s], radius=14 * s, fill=(200, 90, 30))
        # tail
        tail = [(cx + 80, cy + 30), (cx + 135, cy - 10 + math.sin(tail_t * 3) * 10), (cx + 125, cy + 40)]
        d.polygon([(x * s, y * s) for x, y in tail], fill=(230, 120, 40))
        # ears
        for side in (-1, 1):
            ear = [(cx + side * 40, cy - 85), (cx + side * 85, cy - 150), (cx + side * 95, cy - 60)]
            d.polygon([(x * s, y * s) for x, y in ear], fill=(240, 130, 45))
            inner = [(cx + side * 52, cy - 90), (cx + side * 80, cy - 132), (cx + side * 85, cy - 78)]
            d.polygon([(x * s, y * s) for x, y in inner], fill=(255, 190, 150))
        # body
        d.ellipse([(cx - 105) * s, (cy - 110) * s, (cx + 105) * s, (cy + 95) * s], fill=(250, 145, 50),
                  outline=(150, 70, 20), width=5 * s)
        d.ellipse([(cx - 60) * s, (cy - 10) * s, (cx + 60) * s, (cy + 85) * s], fill=(255, 210, 160))
        # eyes
        for side in (-1, 1):
            ex = cx + side * 42
            if blink:
                d.line([(ex - 22) * s, (cy - 40) * s, (ex + 22) * s, (cy - 40) * s], fill=(40, 25, 20), width=6 * s)
            else:
                d.ellipse([(ex - 26) * s, (cy - 70) * s, (ex + 26) * s, (cy - 10) * s], fill="white",
                          outline=(40, 25, 20), width=4 * s)
                d.ellipse([(ex - 11 + side * 3) * s, (cy - 50) * s, (ex + 11 + side * 3) * s, (cy - 24) * s],
                          fill=(30, 20, 20))
                d.ellipse([(ex - 3 + side * 3) * s, (cy - 46) * s, (ex + 4 + side * 3) * s, (cy - 39) * s], fill="white")
        # nose + mouth
        d.ellipse([(cx - 9) * s, (cy - 12) * s, (cx + 9) * s, (cy + 2) * s], fill=(120, 50, 40))
        if talk:
            o = 10 + 10 * mouth_k
            d.ellipse([(cx - 16) * s, (cy + 8) * s, (cx + 16) * s, (cy + 8 + o) * s], fill=(110, 30, 40))
        else:
            d.arc([(cx - 18) * s, (cy - 4) * s, (cx + 18) * s, (cy + 18) * s], 20, 160, fill=(110, 50, 40), width=4 * s)
        # cheeks
        for side in (-1, 1):
            d.ellipse([(cx + side * 70 - 14) * s, (cy - 2) * s, (cx + side * 70 + 14) * s, (cy + 14) * s],
                      fill=(255, 120, 120))
    return supersampled((W, H), fn)


def owl_sprite(t, talk, blink):
    flap = round((t * 16) % (2 * math.pi) / (2 * math.pi) * 10) % 10
    beak = round(abs(math.sin(t * 14)) * 3) if talk else -1
    return _owl(flap, beak, blink)


@functools.lru_cache(maxsize=256)
def _owl(flap_i, beak, blink):
    return draw_owl(flap_i / 10 * 2 * math.pi / 16, beak >= 0, blink, max(0, beak) / 3)


def draw_owl(t, talk, blink, beak_k=0.5):
    W, H = 200, 210

    def fn(d, s):
        flap = math.sin(t * 16) * 25
        cx, cy = 100, 110
        for side in (-1, 1):
            wing = [(cx + side * 45, cy - 10), (cx + side * (90 + abs(flap) * 0.3), cy - 20 + flap * side * 0.4),
                    (cx + side * 55, cy + 45)]
            d.polygon([(x * s, y * s) for x, y in wing], fill=(110, 80, 60))
        d.ellipse([(cx - 60) * s, (cy - 70) * s, (cx + 60) * s, (cy + 75) * s], fill=(150, 110, 80),
                  outline=(80, 55, 40), width=4 * s)
        d.ellipse([(cx - 35) * s, (cy - 5) * s, (cx + 35) * s, (cy + 65) * s], fill=(220, 190, 150))
        for side in (-1, 1):
            ex = cx + side * 26
            d.ellipse([(ex - 22) * s, (cy - 55) * s, (ex + 22) * s, (cy - 11) * s], fill=(255, 220, 90),
                      outline=(80, 55, 40), width=3 * s)
            if blink:
                d.line([(ex - 16) * s, (cy - 33) * s, (ex + 16) * s, (cy - 33) * s], fill=(30, 20, 10), width=5 * s)
            else:
                d.ellipse([(ex - 9) * s, (cy - 42) * s, (ex + 9) * s, (cy - 24) * s], fill=(30, 20, 10))
        beak = 6 + (8 * beak_k if talk else 0)
        d.polygon([(cx - 9) * s, (cy - 12) * s, (cx + 9) * s, (cy - 12) * s, cx * s, (cy + beak) * s], fill=(240, 160, 40))
        for side in (-1, 1):  # ear tufts
            d.polygon([(cx + side * 30) * s, (cy - 60) * s, (cx + side * 55) * s, (cy - 95) * s,
                       (cx + side * 50) * s, (cy - 50) * s], fill=(110, 80, 60))
    return supersampled((W, H), fn)


class CartoonRenderer:
    def __init__(self):
        self.hills = make_hills()
        self.clouds = [(make_cloud(420, 200, i), 0.05 + 0.1 * (i % 3), 18 + 9 * i, i * 520) for i in range(5)]
        # The background is computed at half resolution (smooth gradients) and upscaled.
        self.bw, self.bh, self.bhor = PIC_W // 2, PIC_H // 2, HORIZON // 2
        self.sky_k = np.linspace(0, 1, self.bhor)[:, None, None]
        self.ys = np.arange(self.bh)[:, None]
        self.xs = np.arange(self.bw)[None, :]
        self.birds = [(random.Random(i).uniform(0, 1), random.Random(i + 9).uniform(0.12, 0.3)) for i in range(6)]

    def background(self, t):
        top = np.array(keyframes(t, SKY_TOP), dtype=np.float32)
        hor = np.array(keyframes(t, SKY_HOR), dtype=np.float32)
        img = np.zeros((self.bh, self.bw, 3), dtype=np.float32)
        img[:self.bhor] = top * (1 - self.sky_k) + hor * self.sky_k
        # sun with glow
        sy = keyframes(t, SUN_Y) * self.bh
        sx = self.bw * 0.5
        r = 60.0
        d = np.sqrt((self.xs - sx) ** 2 + (self.ys - sy) ** 2)
        glow = np.clip(1.4 - d / (r * 3.2), 0, 1)[..., None] ** 2
        img = img * (1 - 0.55 * glow) + np.array([255, 220, 150], np.float32) * 0.55 * glow
        disc = np.clip(r - d + 0.5, 0, 1)[..., None]
        img = img * (1 - disc) + np.array([255, 235, 160], np.float32) * disc
        # sea (below horizon): darker gradient + reflection path
        sea_k = np.linspace(0, 1, self.bh - self.bhor)[:, None, None]
        sea = hor * 0.55 * (1 - sea_k) + np.array([20, 25, 60], np.float32) * sea_k
        rows = np.arange(self.bh - self.bhor)[:, None]
        shimmer = (np.sin(rows * 1.8 + t * 3) * np.sin(self.xs * 0.1 + rows * 0.6 + t) > 0.55)
        width = 45 + rows * 1.4
        path = (np.abs(self.xs - sx) < width) & shimmer
        sea = np.where(path[..., None], np.array([255, 210, 140], np.float32), sea)
        img[self.bhor:] = sea
        return Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((PIC_W, PIC_H), Image.BILINEAR)

    def frame(self, t):
        pic = self.background(t).convert("RGBA")
        for cloud, ypos, speed, off in self.clouds:
            x = int((off + t * speed) % (PIC_W + 500)) - 450
            pic.alpha_composite(cloud, (x, int(PIC_H * ypos)))
        pic.alpha_composite(self.hills)
        d = ImageDraw.Draw(pic)
        for bx, by in self.birds:  # seagulls
            x = ((bx + t * 0.02) % 1.2 - 0.1) * PIC_W
            y = by * PIC_H + math.sin(t * 2 + bx * 10) * 10
            f = math.sin(t * 8 + bx * 20) * 8
            d.line([(x - 18, y - f), (x, y), (x + 18, y - f)], fill=(40, 30, 50), width=4)
        blink = (t % 4.1) < 0.12
        zx, zy = keyframes(t, Z_X) * PIC_W, keyframes(t, Z_Y) * PIC_H
        walking = abs(keyframes(t + 0.1, Z_X) - keyframes(t, Z_X)) > 0.0005
        z = zakatun_sprite(t, walking, speaking(t, "z"), blink)
        pic.alpha_composite(z, (int(zx - z.width / 2), int(zy - z.height * 0.8)))
        ox, oy = keyframes(t, O_X) * PIC_W, keyframes(t, O_Y) * PIC_H
        if -0.1 < ox / PIC_W < 1.1:
            o = owl_sprite(t, speaking(t, "o"), (t % 3.3) < 0.12)
            pic.alpha_composite(o, (int(ox - o.width / 2), int(oy - o.height / 2 + math.sin(t * 3) * 8)))
        # fade to dusk at the very end
        if t > 176:
            k = smooth((t - 176) / 4) * 0.85
            pic = Image.blend(pic, Image.new("RGBA", pic.size, (0, 0, 0, 255)), k)
        frame = Image.new("RGB", (FRAME_W, FRAME_H), (0, 0, 0))
        frame.paste(pic.convert("RGB"), (0, BAR))
        return frame


def write_srt(path: Path):
    def ts(x):
        ms = int(round(x * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    lines = []
    for i, (s, e, _, text) in enumerate(DIALOGUE, 1):
        lines += [str(i), f"{ts(s)} --> {ts(e)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def encode(frames, total, size, fps, out: Path, audio_filter: str, duration: float):
    w, h = size
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-f", "lavfi", "-i", audio_filter,
           "-map", "0:v", "-map", "1:a", "-t", f"{duration}",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i, img in enumerate(frames):
        proc.stdin.write(img.tobytes())
        if i % (fps * 10) == 0:
            print(f"  {out.name}: {i / total * 100:4.0f}%", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {out}")


_renderer = None


def _init_worker():
    global _renderer
    _renderer = CartoonRenderer()


def _cartoon_frame(i):
    return _renderer.frame(i / FPS).tobytes()


class _Raw:
    def __init__(self, data):
        self.data = data

    def tobytes(self):
        return self.data


def make_cartoon(out_dir: Path):
    n = int(DURATION * FPS)
    pool = mp.Pool(max(1, (mp.cpu_count() or 2)), initializer=_init_worker)
    frames = (_Raw(b) for b in pool.imap(_cartoon_frame, range(n), chunksize=6))
    # soft music bed (no voice): slow chords
    music = ("aevalsrc='0.05*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.25*t))+0.04*sin(2*PI*277*t)"
             "+0.035*sin(2*PI*330*t)*(0.5+0.5*sin(2*PI*0.125*t))':s=48000:d=" + str(DURATION))
    encode(frames, n, (FRAME_W, FRAME_H), FPS, out_dir / "zakatun.mp4", music, DURATION)
    pool.close()
    write_srt(out_dir / "zakatun.srt")


# ---------------------------------------------------------------------------
# Obby gameplay (pseudo-3D)
# ---------------------------------------------------------------------------

GW, GH = 1280, 720
G_FPS = 30
G_DUR = 40.0
SPEED = 6.0                       # world units per second
PATTERN = [(0, 9, 0.0, 6), (11.5, 5, -1.8, 3), (18.5, 4, 1.6, 3), (25, 6, 0.0, 4), (33, 3, -1.2, 2.5),
           (38.5, 7, 0.0, 5)]     # (z_start, length, x_offset, width)
PATTERN_LEN = 48.0                # SPEED * G_DUR = 240 = 5 * 48 → seamless loop
CAM_H, FOCAL, HZ = 3.2, 620.0, 250


def project(x, y, z):
    z = max(z, 0.3)
    return GW / 2 + FOCAL * x / z, HZ + FOCAL * (CAM_H - y) / z


def platforms_near(cam_z, far=70):
    out = []
    base = math.floor(cam_z / PATTERN_LEN) * PATTERN_LEN
    for k in range(-1, 3):
        for z0, ln, xo, w in PATTERN:
            zs = base + k * PATTERN_LEN + z0
            if zs + ln > cam_z + 0.4 and zs < cam_z + far:
                out.append((zs, zs + ln, xo, w))
    return sorted(out, key=lambda p: -p[0])


def runner_height(zc):
    """Jump arcs over gaps between consecutive platforms."""
    z = zc % PATTERN_LEN
    plats = [(z0, z0 + ln) for z0, ln, _, _ in PATTERN] + [(PATTERN_LEN, PATTERN_LEN + 9)]
    for (a0, a1), (b0, _) in zip(plats, plats[1:]):
        j0, j1 = a1 - 0.8, b0 + 0.8
        if j0 <= z <= j1:
            k = (z - j0) / (j1 - j0)
            return 4 * 1.6 * k * (1 - k)
    return 0.0


def draw_runner(d, cx, by, t, airborne, s=1.0):
    swing = 0 if airborne else math.sin(t * 14) * 14
    lw, lh = 22 * s, 46 * s
    for dx, sw in ((-14, swing), (14, -swing)):
        d.polygon([(cx + dx * s - lw / 2, by - lh), (cx + dx * s + lw / 2, by - lh),
                   (cx + dx * s + lw / 2 + sw * s, by), (cx + dx * s - lw / 2 + sw * s, by)], fill=(70, 160, 70))
    d.rectangle([cx - 30 * s, by - lh - 52 * s, cx + 30 * s, by - lh], fill=(40, 90, 210))  # torso
    for side in (-1, 1):
        a = -swing if side > 0 else swing
        d.polygon([(cx + side * 30 * s, by - lh - 50 * s), (cx + side * 46 * s, by - lh - 50 * s),
                   (cx + side * 46 * s + a * 0.6 * s, by - lh - 6 * s), (cx + side * 30 * s + a * 0.6 * s, by - lh - 6 * s)],
                  fill=(250, 210, 60))
    d.rectangle([cx - 22 * s, by - lh - 92 * s, cx + 22 * s, by - lh - 52 * s], fill=(250, 210, 60))  # head (from behind)


def gameplay_frame(t):
    img = Image.new("RGB", (GW * 2, GH * 2))
    d = ImageDraw.Draw(img)
    S = 2
    for y in range(0, HZ * S, 4):  # sky
        k = y / (HZ * S)
        d.rectangle([0, y, GW * S, y + 4], fill=(int(lerp(120, 205, k)), int(lerp(180, 230, k)), 255))
    d.rectangle([0, HZ * S, GW * S, GH * S], fill=(140, 200, 240))
    cam_z = t * SPEED
    for z0, z1, xo, w in platforms_near(cam_z):
        a0, a1 = max(z0 - cam_z, 0.4), z1 - cam_z
        rows = max(2, int((a1 - a0) / 1.0))
        for r in range(rows):
            za, zb = a0 + (a1 - a0) * r / rows, a0 + (a1 - a0) * (r + 1) / rows
            for c in range(4):
                xa, xb = xo - w / 2 + w * c / 4, xo - w / 2 + w * (c + 1) / 4
                quad = [project(xa, 0, za), project(xb, 0, za), project(xb, 0, zb), project(xa, 0, zb)]
                checker = (int(math.floor(z0 + (za + cam_z - z0))) + c) % 2
                col = (150, 70, 210) if checker else (120, 50, 185)
                d.polygon([(x * S, y * S) for x, y in quad], fill=col)
        front = [project(xo - w / 2, 0, a0), project(xo + w / 2, 0, a0), project(xo + w / 2, -1.2, a0),
                 project(xo - w / 2, -1.2, a0)]
        d.polygon([(x * S, y * S) for x, y in front], fill=(55, 30, 90))
    h = runner_height(cam_z + 4.0)
    rx, ry = project(0, h, 4.0)
    _, gy = project(0, 0, 4.0)
    d.ellipse([(rx - 40) * S, (gy - 8) * S, (rx + 40) * S, (gy + 8) * S], fill=(60, 40, 90))  # shadow
    draw_runner(d, rx * S, ry * S, t, h > 0.05, S * 1.25)
    # HUD: progress bar + skip button (generic obby UI)
    prog = (cam_z % PATTERN_LEN) / PATTERN_LEN
    bx0, bx1, by0, by1 = 380, 900, 22, 58
    d.rounded_rectangle([bx0 * S, by0 * S, bx1 * S, by1 * S], radius=12 * S, fill=(30, 30, 40), outline="white", width=3 * S)
    d.rounded_rectangle([(bx0 + 4) * S, (by0 + 4) * S, (bx0 + 4 + (bx1 - bx0 - 8) * prog) * S, (by1 - 4) * S],
                        radius=9 * S, fill=(90, 220, 90))
    d.rounded_rectangle([24 * S, 22 * S, 120 * S, 58 * S], radius=10 * S, fill=(40, 40, 60), outline="white", width=3 * S)
    d.polygon([(44 * S, 30 * S), (64 * S, 40 * S), (44 * S, 50 * S)], fill="white")
    d.polygon([(64 * S, 30 * S), (84 * S, 40 * S), (64 * S, 50 * S)], fill="white")
    return img.reduce(2)


def make_gameplay(out_dir: Path):
    n = int(G_DUR * G_FPS)
    frames = (gameplay_frame(i / G_FPS) for i in range(n))
    sfx = "anoisesrc=color=pink:amplitude=0.02:d=" + str(G_DUR)
    encode(frames, n, (GW, GH), G_FPS, out_dir / "obby_gameplay.mp4", sfx, G_DUR)


def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("Нужен FFmpeg в PATH")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "output")
    out.mkdir(parents=True, exist_ok=True)
    print("Геймплей…")
    make_gameplay(out)
    print("Мультфильм «Закатун»…")
    make_cartoon(out)
    print(f"Готово: {out}")


if __name__ == "__main__":
    main()
