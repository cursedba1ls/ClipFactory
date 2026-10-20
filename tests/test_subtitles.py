"""Subtitle cue building, remapping across episode boundaries and ASS/SRT output."""
import re

import pytest

from backend.services import subtitle_generator as sg


def words_from(text, start, step=0.4, dur=0.3):
    out, t = [], start
    for w in text.split():
        out.append({"start": round(t, 3), "end": round(t + dur, 3), "text": w})
        t += step
    return out


def test_build_cues_word_limits_and_never_early():
    words = words_from("Привет друзья сегодня мы посмотрим очень интересный мультфильм про кота.", 1.0)
    cues = sg.build_cues(words, max_words=4, max_chars=36)
    assert all(1 <= len(c["words"]) <= 5 for c in cues)
    assert " ".join(c["text"] for c in cues) == " ".join(w["text"] for w in words)
    for c in cues:
        assert c["start"] == c["words"][0]["start"]  # never before speech
        assert c["end"] >= c["words"][-1]["end"]
    for a, b in zip(cues, cues[1:]):
        assert a["end"] <= b["start"] + 1e-9


def test_sentence_punctuation_breaks_cue():
    words = words_from("Это конец. Новое начало", 0)
    cues = sg.build_cues(words, max_words=5)
    assert cues[0]["text"] == "Это конец."


def test_pause_breaks_cue():
    words = words_from("раз два", 0) + words_from("три четыре", 5)
    cues = sg.build_cues(words, max_words=5)
    assert [c["text"] for c in cues] == ["раз два", "три четыре"]


def test_remap_to_episode_local_time():
    words = words_from("one two three four five six", 58.0, step=0.5, dur=0.4)
    # 58.0, 58.5, 59.0, 59.5 | 60.0, 60.5  (boundary at 60)
    ep0 = sg.words_for_episode(words, 0, 60, True, False)
    ep1 = sg.words_for_episode(words, 60, 120, False, True)
    assert [w["text"] for w in ep0] == ["one", "two", "three", "four"]
    assert [w["text"] for w in ep1] == ["five", "six"]
    assert ep1[0]["start"] == pytest.approx(0.0)
    assert ep1[1]["start"] == pytest.approx(0.5)


def test_word_crossing_boundary_goes_to_majority_side_and_is_clipped():
    words = [{"start": 59.7, "end": 60.5, "text": "длинное"}, {"start": 59.0, "end": 60.2, "text": "слово"}]
    ep0 = sg.words_for_episode(words, 0, 60, True, False)
    ep1 = sg.words_for_episode(words, 60, 120, False, True)
    assert [w["text"] for w in ep0] == ["слово"]
    assert ep0[0]["end"] <= 60.0
    assert [w["text"] for w in ep1] == ["длинное"]
    # Shown from 0 in the next episode: the remaining speech is audible there, never earlier.
    assert ep1[0]["start"] == 0.0
    assert ep1[0]["end"] == pytest.approx(0.5)


def test_cues_never_before_speech_across_all_episodes():
    words = words_from(" ".join(f"w{i}" for i in range(400)), 0.2, step=0.37, dur=0.3)
    bounds = [(0, 60), (60, 120), (120, 150)]
    seen = []
    for i, (s, e) in enumerate(bounds):
        cues = sg.cues_for_episode(words, s, e, i == 0, i == len(bounds) - 1)
        for c in cues:
            assert 0 <= c["start"] < c["end"] <= e - s + 1e-9
            first = next(x for x in words if x["text"] == c["words"][0]["text"])
            # never before the first word is spoken (a word started before the boundary is shown from 0)
            assert c["start"] + s >= first["start"] - 1e-6 or (first["start"] < s and c["start"] == 0)
            seen += [w["text"] for w in c["words"]]
    expected = [w["text"] for w in words if w["start"] < 150]
    assert seen == expected  # every word exactly once, in order


def test_ass_contains_styles_cues_and_label():
    cues = sg.build_cues(words_from("Привет мир это тест", 0.5), max_words=4)
    ass = sg.build_ass(cues, {"preset": "classic"}, {"format": "Часть {n}/{total}"}, 2, 5, 60.0, 960)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Sub,Arial,82,&H00FFFFFF" in ass
    assert "Привет мир\\Nэто тест" in ass  # balanced two-line break
    assert "Часть 2/5" in ass
    assert re.search(r"\\pos\(540,960\)", ass)


def test_ass_time_rounds_start_up():
    assert sg.ass_time(1.234, round_up=True) == "0:00:01.24"
    assert sg.ass_time(1.234) == "0:00:01.23"
    assert sg.ass_time(3725.5) == "1:02:05.50"


def test_karaoke_and_animated_presets_emit_word_events():
    cue = {"start": 1.0, "end": 2.2, "text": "раз два три",
           "words": [{"start": 1.0, "end": 1.3, "text": "раз"}, {"start": 1.4, "end": 1.7, "text": "два"},
                     {"start": 1.8, "end": 2.1, "text": "три"}]}
    k = sg.build_ass([cue], {"preset": "karaoke"}, {"enabled": False}, 1, 1, 10, 960)
    events = [l for l in k.splitlines() if l.startswith("Dialogue")]
    assert len(events) == 3
    assert "&H00E6FF&" in events[1]  # highlight colour (#FFE600 in BGR)
    assert events[0].split(",")[1] == "0:00:01.00"
    a = sg.build_ass([cue], {"preset": "animated"}, {"enabled": False}, 1, 1, 10, 960)
    events = [l for l in a.splitlines() if l.startswith("Dialogue")]
    assert len(events) == 3 and "\\alpha&HFF&" in events[0] and "\\t(" in events[0]


def test_edited_text_with_different_word_count_still_renders():
    cue = {"start": 0.0, "end": 2.0, "text": "совсем новый текст тут", "words": [{"start": 0, "end": 1, "text": "x"}]}
    words = sg.cue_words(cue)
    assert len(words) == 4 and words[0]["start"] == 0.0 and words[-1]["end"] == pytest.approx(2.0)


def test_label_moves_away_from_subtitles():
    st = sg.resolve_style({"preset": "classic", "position": 6})  # subtitles at the very top
    nb = sg.resolve_numbering({"position": "top-center"})
    assert sg.resolve_label_position(nb, st, 0.06 * 1920, True) == "bottom-center"
    st2 = sg.resolve_style({"preset": "classic"})
    assert sg.resolve_label_position(nb, st2, 960, True) == "top-center"


def test_two_line_split():
    assert sg.split_lines(["короткий"], 18) is None
    br = sg.split_lines("очень длинная строка субтитров".split(), 18)
    assert br == 2


def test_srt_output():
    srt = sg.build_srt([{"start": 0, "end": 1.5, "text": "Привет"}, {"start": 61.25, "end": 62, "text": "Мир"}])
    assert "1\n00:00:00,000 --> 00:00:01,500\nПривет" in srt
    assert "00:01:01,250 --> 00:01:02,000" in srt


def test_sanitize_cues_clamps_and_sorts():
    cues = sg.sanitize_cues([{"start": 5, "end": 99, "text": " b "}, {"start": 1, "end": 2, "text": "a"},
                             {"start": 3, "end": 2, "text": "bad"}, {"start": 0, "end": 1, "text": "   "}], 60)
    assert [c["text"] for c in cues] == ["a", "b"]
    assert cues[1]["end"] == 60


def test_style_escaping():
    assert "{" not in sg.ass_escape("{\\b1}hack")
    st = sg.resolve_style({"font_family": "Arial,{evil}"})
    assert "," not in st["font_family"] and "{" not in st["font_family"]


def test_bigword_preset_is_default_and_short():
    st = sg.resolve_style(None)
    assert st["preset"] == "bigword" and st["max_words"] == 2 and st["font_size"] >= 110
    words = words_from("аа лысая башка ты куда идёшь сегодня утром", 0.0, step=0.3, dur=0.25)
    cues = sg.cues_for_episode(words, 0, 60, True, True, None)
    assert all(1 <= len(c["words"]) <= 2 for c in cues)
    ass = sg.build_ass(cues, None, {"enabled": False}, 1, 1, 60, 960)
    assert "Style: Sub,Arial Black,124" in ass and "\\pos(540,960)" in ass
