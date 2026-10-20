"""Gameplay looping and selection."""
import pytest

from backend.services.gameplay_manager import MIN_PIECE, choose_gameplay, plan_pieces


def _check_pieces(pieces, us, ue, need):
    assert sum(p.duration for p in pieces) >= need - 1e-6
    for p in pieces:
        assert p.duration >= MIN_PIECE - 1e-9
        assert us - 1e-9 <= p.start and p.start + p.duration <= ue + 1e-6


def test_short_gameplay_loops_for_long_episode():
    # 2-minute gameplay, 10-minute main video → episodes must still be covered
    pieces = plan_pieces(0, 120, offset=100, duration=600)
    _check_pieces(pieces, 0, 120, 600)
    assert pieces[0].start == 100 and pieces[0].duration == 20
    assert pieces[1].start == 0


def test_usable_range_excludes_black_edges():
    pieces = plan_pieces(1.5, 30.0, offset=0, duration=95)
    _check_pieces(pieces, 1.5, 30.0, 95)
    assert all(p.start >= 1.5 for p in pieces)


def test_offset_wraps():
    pieces = plan_pieces(0, 10, offset=25, duration=3)
    assert pieces[0].start == pytest.approx(5)


def test_tiny_leftover_skipped():
    pieces = plan_pieces(0, 10, offset=9.9, duration=5)
    assert pieces[0].start == 0  # 0.1 s leftover is shorter than MIN_PIECE → start next loop


def test_continuous_mode_walks_through_clip():
    clips = [{"id": "a", "usable_start": 0, "usable_end": 150}]
    choices = choose_gameplay(clips, [60, 60, 60, 60], "continuous")
    assert [c.offset for c in choices] == [0, 60, 120 % 150, 30]


def test_random_mode_offsets_fit_and_reproducible():
    clips = [{"id": "a", "usable_start": 0, "usable_end": 300}, {"id": "b", "usable_start": 2, "usable_end": 100}]
    c1 = choose_gameplay(clips, [60] * 10, "random", seed=7)
    c2 = choose_gameplay(clips, [60] * 10, "random", seed=7)
    assert c1 == c2
    assert {c.media_id for c in c1} == {"a", "b"}
    for c in c1:
        length = 300 if c.media_id == "a" else 98
        assert 0 <= c.offset <= max(0, length - 60)


def test_multiple_clips_continuous_rotates():
    clips = [{"id": "a", "usable_start": 0, "usable_end": 100}, {"id": "b", "usable_start": 0, "usable_end": 100}]
    choices = choose_gameplay(clips, [60, 60, 60], "continuous")
    assert choices[0].media_id == "a"
    assert choices[1].media_id == "b"
