"""File helpers that behave well on Windows."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def safe_replace(src: Path, dst: Path, attempts: int = 10, delay: float = 0.5) -> Path:
    """Atomically move src over dst; returns the final path.

    On Windows a file that is open elsewhere (e.g. the video playing in the browser preview)
    cannot be replaced. Retry for a few seconds, then fall back to a fresh file name so the
    render is never lost.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return dst
        except PermissionError:
            if i == attempts - 1:
                break
            time.sleep(delay)
    alt = dst.with_name(f"{dst.stem}-{int(time.time())}{dst.suffix}")
    log.warning("%s is in use, saving as %s", dst.name, alt.name)
    os.replace(src, alt)
    return alt
