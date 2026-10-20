"""Gameplay selection and looping.

Gameplay is secondary visual filler. For every episode we pick a clip and a
start offset, then cover the episode duration with one or more consecutive
pieces of the clip's usable range (black edges excluded), looping as needed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

MIN_PIECE = 0.25  # seconds; shorter leftovers are skipped to avoid empty segments
SAFETY_MARGIN = 0.5  # extra gameplay beyond the episode end, trimmed in the filter graph


@dataclass(frozen=True)
class GameplayPiece:
    start: float  # absolute position in the gameplay file
    duration: float


def plan_pieces(usable_start: float, usable_end: float, offset: float, duration: float,
                margin: float = SAFETY_MARGIN) -> list[GameplayPiece]:
    """Cover `duration` seconds starting at `offset` within the usable range, looping.

    offset is relative to usable_start. Returned pieces are absolute file times.
    """
    length = usable_end - usable_start
    if length <= MIN_PIECE:
        raise ValueError("gameplay clip is too short")
    if duration <= 0:
        return []
    need = duration + margin
    pos = usable_start + (offset % length)
    pieces: list[GameplayPiece] = []
    covered = 0.0
    guard = 0
    while covered < need - 1e-6:
        guard += 1
        if guard > 10000:
            raise RuntimeError("gameplay plan did not converge")
        available = usable_end - pos
        if available < MIN_PIECE:
            pos = usable_start
            continue
        take = min(available, need - covered)
        if take < MIN_PIECE and pieces:
            # Extend coverage a little instead of creating a tiny tail piece.
            take = min(available, MIN_PIECE)
        pieces.append(GameplayPiece(round(pos, 6), round(take, 6)))
        covered += take
        pos += take
        if usable_end - pos < MIN_PIECE:
            pos = usable_start
    return pieces


@dataclass(frozen=True)
class GameplayChoice:
    media_id: str
    offset: float  # relative to the clip's usable start


def choose_gameplay(
    clips: list[dict],
    durations: list[float],
    mode: str = "continuous",
    seed: int = 0,
) -> list[GameplayChoice]:
    """Pick a gameplay clip and offset for each episode.

    clips: [{"id", "usable_start", "usable_end"}]; must be non-empty.
    mode:
      continuous — walk through the clip(s) sequentially, so consecutive
                   episodes reuse gameplay as little as possible;
      random     — random clip (if several) and random start offset per episode.
    """
    if not clips:
        raise ValueError("no gameplay clips")
    rng = random.Random(seed)
    choices: list[GameplayChoice] = []
    if mode == "random":
        for d in durations:
            clip = rng.choice(clips)
            length = clip["usable_end"] - clip["usable_start"]
            max_off = max(0.0, length - d)
            choices.append(GameplayChoice(clip["id"], round(rng.uniform(0.0, max_off), 3)))
        return choices
    # continuous
    n = len(clips)
    clip_idx = 0
    pos = 0.0
    for d in durations:
        clip = clips[clip_idx % n]
        length = clip["usable_end"] - clip["usable_start"]
        if n > 1 and pos > 0 and pos + d > length:
            # Several clips: move to the next one instead of looping the current clip.
            clip_idx += 1
            pos = 0.0
            clip = clips[clip_idx % n]
            length = clip["usable_end"] - clip["usable_start"]
        if pos >= length - MIN_PIECE:
            pos = 0.0
        choices.append(GameplayChoice(clip["id"], round(pos, 3)))
        pos += d
        if n == 1 and length > 0:
            pos %= length
    return choices
