"""Speech recognition resilience: failing parts are skipped, GPU/VAD failures recover, errors stay readable."""
import math
import wave
from types import SimpleNamespace

import numpy as np
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
        self.inputs = []
        self.device = device

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        self.inputs.append(audio)
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
    monkeypatch.setattr(sg, "load_audio", lambda path: np.zeros(20 * sg.SAMPLE_RATE, np.float32))

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
    monkeypatch.setattr(sg, "load_audio", lambda path: np.zeros(20 * sg.SAMPLE_RATE, np.float32))
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


def test_engine_gets_samples_not_a_file_path(patch_engine):
    """faster-whisper's own file decoder (PyAV) is never used: it breaks with some PyAV versions."""
    model = patch_engine(ScriptedModel(SEGMENTS))
    sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert all(isinstance(a, np.ndarray) and a.dtype == np.float32 for a in model.inputs)


def test_error_before_start_is_reported_once_not_retried_per_second(patch_engine):
    """The user's case: an error that repeats on every attempt must not loop 40 times."""
    message = "open() got an unexpected keyword argument 'metadata_errors'"
    model = patch_engine(ScriptedModel(SEGMENTS, call_errors=[message] * 100))
    with pytest.raises(UserError) as exc:
        sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert len(model.calls) == 1
    assert exc.value.message == "Ошибка распознавания речи."
    assert message in exc.value.detail and "ни в одном участке" not in exc.value.detail


def test_error_before_restart_keeps_already_recognised_words(patch_engine):
    model = patch_engine(ScriptedModel(SEGMENTS, fail_at=8.0))
    original = model.transcribe

    def transcribe(audio, **kw):
        if kw.get("clip_timestamps"):
            raise RuntimeError("cannot restart")
        return original(audio, **kw)
    model.transcribe = transcribe
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert {w["start"] for w in result["words"]} >= {0.0, 6.0}
    assert result["skipped"][-1]["end"] == pytest.approx(20.0)


def test_words_after_the_end_of_audio_are_dropped(patch_engine):
    segs = SEGMENTS + [(25.0, 26.0, "призрак")]
    patch_engine(ScriptedModel(segs))
    result = sg.transcribe("x.wav", device="cpu", duration=20.0)
    assert all(w["start"] < 20.0 and w["end"] <= 20.0 for w in result["words"])
    assert "призрак" not in {w["text"] for w in result["words"]}


def _write_wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype("<i2").tobytes())


def test_load_audio_reads_the_extracted_wav(tmp_path):
    p = tmp_path / "speech.wav"
    _write_wav(p, np.array([0, 16384, -32768, 32767], dtype=np.int16))
    a = sg.load_audio(p)
    assert a.dtype == np.float32
    assert a.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])


def test_load_audio_mixes_stereo(tmp_path):
    p = tmp_path / "stereo.wav"
    _write_wav(p, np.array([16384, 0, 16384, 16384], dtype=np.int16), channels=2)
    assert sg.load_audio(p).tolist() == pytest.approx([0.25, 0.5])


def test_load_audio_converts_other_formats_with_ffmpeg(tmp_path):
    from tests.conftest import ffmpeg
    p = tmp_path / "tone.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=2:r=44100", "-c:a", "aac", str(p))
    a = sg.load_audio(p)
    assert a.dtype == np.float32
    assert len(a) == pytest.approx(2 * sg.SAMPLE_RATE, abs=1600)
    assert 0.05 < float(np.abs(a).max()) <= 1.0
