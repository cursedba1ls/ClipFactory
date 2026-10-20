"""Episode boundary calculation and full-timeline preservation."""
import random
from fractions import Fraction

import pytest

from backend.errors import UserError
from backend.services.video_splitter import cut_window, plan_episodes, validate_coverage


def test_five_minutes_gives_five_full_episodes():
    fps = Fraction(30)
    spans = plan_episodes(300 * 30, fps, 60)
    assert len(spans) == 5
    assert [s.duration for s in spans] == [60.0] * 5
    assert [(s.start_time, s.end_time) for s in spans] == [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300)]
    validate_coverage(spans, 300 * 30)


def test_five_minutes_23_seconds_gives_six_episodes_last_23s():
    fps = Fraction(25)
    spans = plan_episodes(323 * 25, fps, 60)
    assert len(spans) == 6
    assert all(s.duration == 60 for s in spans[:5])
    assert spans[-1].duration == pytest.approx(23.0)
    validate_coverage(spans, 323 * 25)


@pytest.mark.parametrize("seconds", [30, 45, 60, 90, 37.5])
def test_episode_durations(seconds):
    fps = Fraction(24)
    total = 24 * 600
    spans = plan_episodes(total, fps, seconds, min_last_seconds=0)
    validate_coverage(spans, total)
    for s in spans[:-1]:
        assert s.duration == pytest.approx(seconds, abs=1 / 24)


def test_tiny_remainder_is_merged_not_dropped():
    fps = Fraction(30)
    total = 30 * 121  # 2:01
    spans = plan_episodes(total, fps, 60, min_last_seconds=3)
    assert len(spans) == 2
    assert spans[-1].duration == pytest.approx(61)
    validate_coverage(spans, total)


def test_short_video_single_episode():
    spans = plan_episodes(30 * 20, Fraction(30), 60)
    assert len(spans) == 1 and spans[0].duration == 20


def test_ntsc_rates_no_gaps_or_overlaps_randomized():
    rng = random.Random(42)
    for _ in range(300):
        fps = rng.choice([Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001),
                          Fraction(25), Fraction(50), Fraction(12)])
        total = rng.randint(1, 200000)
        secs = rng.choice([30, 45, 60, 90, rng.uniform(5, 300)])
        spans = plan_episodes(total, fps, secs, min_last_seconds=rng.choice([0, 3, 10]))
        validate_coverage(spans, total)
        assert sum(s.frames for s in spans) == total
        # consecutive times are identical floats (no drift between end and next start)
        for a, b in zip(spans, spans[1:]):
            assert a.end_time == b.start_time


def test_invalid_durations():
    with pytest.raises(UserError):
        plan_episodes(100, Fraction(25), 2)
    with pytest.raises(UserError):
        plan_episodes(0, Fraction(25), 60)


def test_cut_window_half_frame_offsets():
    fps = Fraction(30)
    spans = plan_episodes(30 * 180, fps, 60)
    w0 = cut_window(spans[0], fps)
    assert w0.seek == 0 and w0.video_trim_start == 0 and w0.audio_start == 0
    w1 = cut_window(spans[1], fps)
    half = 1 / 60
    assert w1.seek == pytest.approx(59.0)
    assert w1.video_trim_start == pytest.approx(1.0 - half)
    assert w1.video_trim_end == pytest.approx(61.0 - half)
    assert w1.audio_start == pytest.approx(1.0)
    assert w1.audio_end - w1.audio_start == pytest.approx(60.0)
    # stream offset t0 shifts the whole window
    w1b = cut_window(spans[1], fps, t0=0.5)
    assert w1b.seek == pytest.approx(59.5)
