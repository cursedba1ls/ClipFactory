"""Speech recognition resilience: failing parts are skipped, GPU/VAD failures recover, errors stay readable."""
import math
from types import SimpleNamespace

import pytest

from backend.errors import UserError
from backend.services import subtitle_generator as sg


class Word(SimpleNamespace):
    pass


class Seg(SimpleNamespace):
    pass


class ScriptedModel:
    """Behaves like faster-whisper: errors surface while iterating or when the call is made."""

    def __init__(self, segments, fail_at=None, fail_msg="engine failure", call_errors=(), device="cpu"):
        self.segments = segments          # [(start, end, "text")]
        self.fail_at = fail_at            # raise once, when a segment starts at/after this time
        self.fail_msg = fail_msg
        self.call_errors = list(call_errors)
        self.calls = []
        self.device = device

    def transcribe(self, path, **kw):
        self.calls.append(kw)
        if self.call_errors:
            raise RuntimeError(self.call_errors.pop(0))
        clip = kw.get("clip_timestamps")
        begin = clip[0] if clip else 0.0

        def gen():
            for start, end, text in self.segments:
                if start < begin - 1e-6:
                    continue
                if self.fail_at is not None and start >= self.fail_at:
                    self.fail_at = None
                    raise RuntimeError(self.fail_msg)
                words = []
                t = start
                for token in text.split():
                    words.append(Word(word=" " + token, start=t, end=t + 0.4, probability=0.9))
                    t += 0.5
                yield Seg(start=start, end=end, text=text, words=words)

        last_end = max(e for _, e, _ in self.segments)
        return gen(), SimpleNamespace(duration=last_end, language="ru", language_probability=0.98)


SEGMENTS = [(i * 2.0, i * 2.0 + 1.5, f"слово{i}а слово{i}б") for i in range(10)]  # 0..19.5 s


@pytest.fixture()
def patch_engine(monkeypatch):
    monkeypatch.setattr(sg, "whisper_available", lambda: True)

    def install(model, cpu_model=None):
        monkeypatch.setattr(sg, "_load_model", lambda size, device: cpu_model if device == "cpu" and cpu_model else model)
        return model
    return install


def test_failure_in_the_middle_skips_only_the_broken_part(patch_engine):
    model = patch_engine(ScriptedModel(SEGMENTS, fail_at=8.0))
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    starts = sorted({w["start"] for w in result["words"]})
    assert 8.0 not in starts                       # the broken segment is skipped
    assert 0.0 in starts and 10.0 in starts and 18.0 in starts
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["start"] == pytest.approx(7.5)
    assert "RuntimeError: engine failure" in result["skipped"][0]["error"]
    assert len(model.calls) == 2                   # restarted from the next position only
    assert model.calls[1]["clip_timestamps"][0] > 8.0


def test_bad_words_are_skipped_not_fatal(patch_engine):
    seg = Seg(start=0.0, end=2.0, text="привет мир", words=[
        Word(word="привет", start=0.0, end=0.5, probability=0.9),
        Word(word=None, start=0.6, end=0.8, probability=0.9),           # no text
        Word(word="мир", start=float("nan"), end=1.0, probability=0.9),  # broken time
        Word(word="мир", start=1.0, end=0.5, probability=float("nan")),  # end before start
    ])
    model = SimpleNamespace(transcribe=lambda path, **kw: (iter([seg]), SimpleNamespace(duration=2.0, language="ru",
                                                                                          language_probability=0.9)))
    patch_engine(model)
    result = sg.transcribe("x.wav", device="cpu", duration=2.0)
    assert [w["text"] for w in result["words"]] == ["привет", "мир"]
    assert result["words"][1]["end"] >= result["words"][1]["start"]
    assert result["words"][1]["p"] == 0.0


def test_gpu_failure_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(sg, "whisper_available", lambda: True)
    gpu = ScriptedModel(SEGMENTS, call_errors=["Library cublas64_12.dll is not found or cannot be loaded"], device="cuda")
    cpu = ScriptedModel(SEGMENTS)
    loads = []

    def fake_load(size, device):
        loads.append(device)
        return cpu if device == "cpu" else gpu
    monkeypatch.setattr(sg, "_load_model", fake_load)
    result = sg.transcribe("x.wav", device="auto", duration=20.0)
    assert loads == ["auto", "cpu"]
    assert len(result["words"]) == 2 * len(SEGMENTS)
    assert result["skipped"] == []


def test_voice_activity_detection_failure_retries_without_vad(patch_engine):
    model = patch_engine(ScriptedModel(SEGMENTS, call_errors=["onnxruntime: VAD model could not be loaded"]))
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert model.calls[0]["vad_filter"] is True
    assert model.calls[1]["vad_filter"] is False
    assert len(result["words"]) == 2 * len(SEGMENTS)


def test_empty_exception_message_is_still_readable(patch_engine):
    model = patch_engine(ScriptedModel(SEGMENTS, fail_at=0.5, fail_msg=""))
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert result["skipped"][0]["error"].startswith("RuntimeError")


def test_total_failure_shows_the_real_reason(patch_engine):
    class Broken:
        def transcribe(self, path, **kw):
            raise ValueError("unsupported audio format")
    patch_engine(Broken())
    with pytest.raises(UserError) as exc:
        sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert exc.value.message == "Ошибка распознавания речи."
    assert "ValueError: unsupported audio format" in exc.value.detail


def test_clean_run_has_no_skips(patch_engine):
    patch_engine(ScriptedModel(SEGMENTS))
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert result["skipped"] == [] and len(result["segments"]) == len(SEGMENTS)
    assert all(math.isfinite(w["start"]) for w in result["words"])
