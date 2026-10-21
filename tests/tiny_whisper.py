"""A tiny Whisper model with random weights, built locally (no internet).

It cannot recognise words, but it runs through the whole real faster-whisper / CTranslate2 code
path: audio features, voice-activity detection, language detection, decoding, word timestamps.
That is what the tests need to catch integration problems such as the PyAV incompatibility
("open() got an unexpected keyword argument 'metadata_errors'").
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

D_MODEL, HEADS, FFN, N_MELS = 64, 2, 128, 80
LANGUAGES = ["en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar", "sv", "it",
             "id", "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no", "th", "ur",
             "hr", "bg", "lt", "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn", "sr", "az", "sl", "kn",
             "et", "mk", "br", "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq", "sw", "gl", "mr", "pa", "si",
             "km", "sn", "yo", "so", "af", "oc", "ka", "be", "tg", "sd", "gu", "am", "yi", "lo", "uz", "fo",
             "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my", "bo", "tl", "mg", "as", "tt", "haw", "ln", "ha",
             "ba", "jw", "su"]
SPECIALS_AFTER_LANGS = ["<|translate|>", "<|transcribe|>", "<|startoflm|>", "<|startofprev|>",
                        "<|nocaptions|>", "<|notimestamps|>"]
EOT = 50256
VOCAB = 51865  # multilingual Whisper layout: 50257 text tokens, specials, 1501 timestamps


def _byte_chars() -> list[str]:
    """GPT-2 byte-level alphabet (the characters byte-level BPE uses for the 256 bytes)."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + \
        list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return [chr(c) for _, c in sorted(zip(bs, cs))]


def _tokens() -> list[str]:
    text = _byte_chars()
    text += [f"tok{i}" for i in range(EOT - len(text))]
    specials = ["<|endoftext|>", "<|startoftranscript|>"] + [f"<|{c}|>" for c in LANGUAGES] + SPECIALS_AFTER_LANGS
    tokens = text + specials
    tokens += ["<|%.2f|>" % (i * 0.02) for i in range(VOCAB - len(tokens))]
    assert len(tokens) == VOCAB
    return tokens


def _tokenizer_json(tokens: list[str]) -> dict:
    vocab = {t: i for i, t in enumerate(tokens[:EOT])}
    added = [{"id": i, "content": t, "single_word": False, "lstrip": False, "rstrip": False,
              "normalized": False, "special": True} for i, t in enumerate(tokens) if i >= EOT]
    return {
        "version": "1.0", "truncation": None, "padding": None, "added_tokens": added, "normalizer": None,
        "pre_tokenizer": {"type": "ByteLevel", "add_prefix_space": False, "trim_offsets": True, "use_regex": True},
        "post_processor": None,
        "decoder": {"type": "ByteLevel", "add_prefix_space": True, "trim_offsets": True, "use_regex": True},
        "model": {"type": "BPE", "dropout": None, "unk_token": None, "continuing_subword_prefix": "",
                  "end_of_word_suffix": "", "fuse_unk": False, "byte_fallback": False, "vocab": vocab,
                  "merges": []},
    }


def build(dest: str | Path, seed: int = 0) -> Path:
    """Write a loadable faster-whisper model directory; returns its path."""
    from ctranslate2.specs import whisper_spec

    dest = Path(dest)
    if (dest / "model.bin").is_file():
        return dest
    rng = np.random.default_rng(seed)

    def rnd(*shape, scale=0.05):
        return (rng.standard_normal(shape) * scale).astype(np.float32)

    def linear(spec, out_dim, in_dim, bias=True):
        spec.weight = rnd(out_dim, in_dim)
        if bias:
            spec.bias = np.zeros(out_dim, np.float32)

    def norm(spec):
        spec.gamma = np.ones(D_MODEL, np.float32)
        spec.beta = np.zeros(D_MODEL, np.float32)

    def ffn(spec):
        norm(spec.layer_norm)
        linear(spec.linear_0, FFN, D_MODEL)
        linear(spec.linear_1, D_MODEL, FFN)

    spec = whisper_spec.WhisperSpec(1, HEADS, 1, HEADS)
    enc = spec.encoder
    enc.conv1.weight, enc.conv1.bias = rnd(D_MODEL, N_MELS, 3), np.zeros(D_MODEL, np.float32)
    enc.conv2.weight, enc.conv2.bias = rnd(D_MODEL, D_MODEL, 3), np.zeros(D_MODEL, np.float32)
    enc.position_encodings.encodings = rnd(1500, D_MODEL)
    norm(enc.layer_norm)
    for layer in enc.layer:
        norm(layer.self_attention.layer_norm)
        linear(layer.self_attention.linear[0], 3 * D_MODEL, D_MODEL)
        linear(layer.self_attention.linear[1], D_MODEL, D_MODEL)
        ffn(layer.ffn)

    dec = spec.decoder
    dec.embeddings.weight = rnd(VOCAB, D_MODEL)
    dec.position_encodings.encodings = rnd(448, D_MODEL)
    norm(dec.layer_norm)
    for layer in dec.layer:
        norm(layer.self_attention.layer_norm)
        linear(layer.self_attention.linear[0], 3 * D_MODEL, D_MODEL)
        linear(layer.self_attention.linear[1], D_MODEL, D_MODEL)
        norm(layer.attention.layer_norm)
        linear(layer.attention.linear[0], D_MODEL, D_MODEL)
        linear(layer.attention.linear[1], 2 * D_MODEL, D_MODEL)
        linear(layer.attention.linear[2], D_MODEL, D_MODEL)
        ffn(layer.ffn)
    dec.projection.weight = dec.embeddings.weight

    tokens = _tokens()
    spec.register_vocabulary(tokens)
    spec.config.suppress_ids = [-1]
    spec.config.suppress_ids_begin = [220, EOT]
    spec.config.lang_ids = [EOT + 2 + i for i in range(len(LANGUAGES))]
    spec.config.alignment_heads = [(0, h) for h in range(HEADS)]
    spec.validate()
    dest.mkdir(parents=True, exist_ok=True)
    spec.save(str(dest))
    (dest / "tokenizer.json").write_text(json.dumps(_tokenizer_json(tokens), ensure_ascii=False), encoding="utf-8")
    (dest / "preprocessor_config.json").write_text(json.dumps({"feature_size": N_MELS}), encoding="utf-8")
    return dest
