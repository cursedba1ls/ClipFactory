"""Builds the Windows package: dist/ClipFactory/ (ready-to-use folder) and dist/ClipFactory.zip.

    python packaging/build_package.py [--examples DIR]

Layout of the package:
    ClipFactory/
      ClipFactory.exe            launcher (first run installs, then starts the program)
      ПРОЧТИ МЕНЯ.txt
      Инструкции/                step-by-step guides + annotated screenshots (Картинки/)
      Пример/                    example sources and finished parts (if --examples is given)
      Мои видео/                 place for the user's own videos
      app/                       the program itself (git-tracked files)

The launcher is cross-compiled with Zig (pip install ziglang) when no prebuilt exe is present.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "dist"

APP_EXCLUDE_PREFIXES = ("tests/", "packaging/", ".github/", "docs/", "dist/")
GUIDES = ["1. Установка.txt", "2. Первый запуск.txt", "3. Как сделать серию видео.txt", "4. Кадрирование.txt",
          "5. Субтитры.txt", "6. Публикация в TikTok.txt", "7. Если что-то не работает.txt"]


def windows_text(src: Path, dst: Path) -> None:
    """UTF-8 with BOM and CRLF line endings — opens correctly in Windows Notepad."""
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    dst.write_bytes("﻿".encode("utf-8") + text.encode("utf-8"))


def note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("﻿".encode("utf-8") + text.replace("\n", "\r\n").encode("utf-8"))


def build_launcher() -> Path:
    exe = PKG / "launcher" / "ClipFactory.exe"
    src = PKG / "launcher"
    newest_src = max(p.stat().st_mtime for p in src.glob("launcher.*"))
    if exe.is_file() and exe.stat().st_mtime >= newest_src:
        return exe
    cmd = [sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2", "-municode",
           "-Wl,--subsystem,windows", "launcher.c", "launcher.rc", "-o", "ClipFactory.exe"]
    subprocess.run(cmd, cwd=src, check=True)
    (src / "ClipFactory.pdb").unlink(missing_ok=True)
    return exe


def app_files() -> list[str]:
    # -z: raw NUL-separated paths (no quoting of non-ASCII names)
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    files = [f for f in out.decode("utf-8").split("\0") if f]
    return [f for f in files if not f.startswith(APP_EXCLUDE_PREFIXES) and (ROOT / f).is_file()]


def main() -> None:
    # Windows consoles default to cp1252: print Cyrillic safely there.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=Path, help="folder with zakatun.mp4, zakatun.srt, obby_gameplay.mp4 "
                                                   "and finished «Закатун - Часть N.mp4» files")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    out = DIST / "ClipFactory"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    shutil.copy2(build_launcher(), out / "ClipFactory.exe")
    windows_text(PKG / "docs" / "ПРОЧТИ МЕНЯ.txt", out / "ПРОЧТИ МЕНЯ.txt")

    guides = out / "Инструкции"
    (guides / "Картинки").mkdir(parents=True)
    for g in GUIDES:
        windows_text(PKG / "docs" / g, guides / g)
    for img in sorted((PKG / "docs" / "Картинки").glob("*.png")):
        shutil.copy2(img, guides / "Картинки" / img.name)

    app = out / "app"
    for f in app_files():
        src, dst = ROOT / f, app / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        if f.endswith(".bat"):
            dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        else:
            shutil.copy2(src, dst)
    note(app / "Не трогайте эту папку.txt",
         "Здесь находится сама программа ClipFactory.\n"
         "Запускайте её файлом ClipFactory.exe в папке выше.\n\n"
         "Ваши проекты и готовые части хранятся в app\\storage — для резервной копии скопируйте эту папку.\n")

    note(out / "Мои видео" / "Мультфильмы" / "Сюда кладите мультфильмы.txt",
         "Удобное место для мультфильмов, из которых вы делаете серии.\n"
         "В программе: «Новый проект» → «Основное видео» → «Файл с компьютера» → выберите файл отсюда.\n")
    note(out / "Мои видео" / "Геймплей" / "Сюда кладите геймплей.txt",
         "Удобное место для геймплея (например, Roblox) для нижней половины экрана.\n"
         "В программе: «Геймплей» → «Добавить геймплей» → «Файл с компьютера».\n"
         "Один раз добавленный геймплей потом можно выбирать в любом проекте.\n")
    note(out / "Мои видео" / "Готовые видео" / "Сюда сохраняйте готовые части.txt",
         "Кнопка «Скачать» в программе сохраняет видео в папку «Загрузки» браузера.\n"
         "Можно перенести их сюда, чтобы всё было в одном месте.\n")

    if args.examples and args.examples.is_dir():
        ex = out / "Пример"
        ex.mkdir(parents=True)
        windows_text(PKG / "docs" / "Как попробовать пример.txt", ex / "Как попробовать пример.txt")
        (ex / "Исходники").mkdir()
        (ex / "Готовый результат").mkdir()
        for name in ("zakatun.mp4", "zakatun.srt", "obby_gameplay.mp4"):
            if (args.examples / name).is_file():
                shutil.copy2(args.examples / name, ex / "Исходники" / name)
        for f in sorted(args.examples.glob("Закатун - Часть *.mp4")):
            shutil.copy2(f, ex / "Готовый результат" / f.name)

    if not args.no_zip:
        zpath = DIST / "ClipFactory.zip"
        zpath.unlink(missing_ok=True)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    arc = Path("ClipFactory") / p.relative_to(out)
                    comp = zipfile.ZIP_STORED if p.suffix.lower() in (".mp4", ".png", ".jpg") else zipfile.ZIP_DEFLATED
                    z.write(p, arc.as_posix(), compress_type=comp)
        print(f"Архив: {zpath} ({zpath.stat().st_size / 1048576:.1f} МБ)")
    print(f"Папка: {out}")


if __name__ == "__main__":
    main()
