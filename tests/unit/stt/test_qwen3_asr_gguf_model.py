"""Qwen3-ASR GGUF provider: binary resolution, availability gating, subprocess argv.

The engine shells out to the qwen3-asr-cli binary (no Python module), so these
tests fake shutil.which / os.path / subprocess.run rather than a fake module.
"""

import io
import wave

import pytest

from app.services.stt.providers import qwen3_asr_gguf_provider as g


def _silent_wav() -> bytes:
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 1600)
    w.close()
    return b.getvalue()


def test_resolve_binary_prefers_config_path(monkeypatch):
    monkeypatch.setattr(
        g, "resolve_stt_engine_config", lambda _e: {"binary_path": "/opt/qwen/qwen3-asr-cli"}
    )
    # config path exists as a file -> used, PATH never consulted
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/opt/qwen/qwen3-asr-cli")
    monkeypatch.setattr(g.shutil, "which", lambda _p: None)
    assert g.resolve_qwen3_asr_gguf_binary() == "/opt/qwen/qwen3-asr-cli"


def test_resolve_binary_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(g, "resolve_stt_engine_config", lambda _e: {"binary_path": ""})
    # A which() hit implies the file exists, so isfile agrees for the resolved path.
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/usr/local/bin/qwen3-asr-cli")
    monkeypatch.setattr(g.shutil, "which", lambda p: "/usr/local/bin/qwen3-asr-cli" if p == "qwen3-asr-cli" else None)
    assert g.resolve_qwen3_asr_gguf_binary() == "/usr/local/bin/qwen3-asr-cli"


def test_resolve_binary_none_when_absent(monkeypatch):
    monkeypatch.setattr(g, "resolve_stt_engine_config", lambda _e: {"binary_path": ""})
    monkeypatch.setattr(g.os.path, "isfile", lambda _p: False)
    monkeypatch.setattr(g.shutil, "which", lambda _p: None)
    assert g.resolve_qwen3_asr_gguf_binary() is None


def test_available_requires_both_binary_and_model(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_model", lambda: "/models/m.gguf")

    # binary present, model missing -> hidden
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(g.os.path, "isfile", lambda p: False)
    assert p.available() is False

    # both present -> available
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/models/m.gguf")
    assert p.available() is True

    # model present, binary missing -> hidden
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: None)
    assert p.available() is False


def test_transcribe_builds_expected_argv(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(
        g, "resolve_stt_engine_config",
        lambda _e: {"default_model": "/models/m.gguf", "n_threads": 4, "timeout_seconds": 99.0},
    )
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/models/m.gguf")

    captured = {}

    class _Proc:
        stdout = "  xin chào thế giới  \n\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(g.subprocess, "run", _fake_run)

    text = p._transcribe("/tmp/a.wav", "vi")
    assert text == "xin chào thế giới"
    assert captured["argv"] == [
        "/bin/qwen3-asr-cli", "-m", "/models/m.gguf", "-f", "/tmp/a.wav",
        "-t", "4", "--no-timing", "--language", "vi",
    ]
    assert captured["kwargs"]["timeout"] == 99.0
    assert captured["kwargs"]["check"] is True


def test_transcribe_omits_lang_flag_for_unknown_language(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(
        g, "resolve_stt_engine_config",
        lambda _e: {"default_model": "/models/m.gguf", "n_threads": 8, "timeout_seconds": 120.0},
    )
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/models/m.gguf")

    captured = {}

    class _Proc:
        stdout = "hello"
        stderr = ""

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(g.subprocess, "run", _fake_run)

    p._transcribe("/tmp/a.wav", None)  # unknown language -> auto-detect, no --language
    assert "--language" not in captured["argv"]


def test_transcribe_ignores_non_path_model_field(monkeypatch):
    # The OpenAI surface forwards the engine/model *id* (e.g. "qwen3_asr_gguf") in
    # the `model` field, not a GGUF path. It must fall back to the default model,
    # not treat the id as a missing file. (Regression: was raising "model not found".)
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(
        g, "resolve_stt_engine_config",
        lambda _e: {"default_model": "/models/m.gguf", "n_threads": 8, "timeout_seconds": 120.0},
    )
    # only the default model exists on disk; the id "qwen3_asr_gguf" is not a file
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/models/m.gguf")

    captured = {}

    class _Proc:
        stdout = "ok"
        stderr = ""

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(g.subprocess, "run", _fake_run)

    text = p._transcribe("/tmp/a.wav", "vi", model="qwen3_asr_gguf")
    assert text == "ok"
    assert "/models/m.gguf" in captured["argv"]  # fell back to the default
    assert "qwen3_asr_gguf" not in captured["argv"]  # id not passed as -m


def test_transcribe_raises_clean_error_when_binary_missing(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: None)
    with pytest.raises(RuntimeError, match="qwen3-asr-cli binary"):
        p._transcribe("/tmp/a.wav", "vi")


def test_transcribe_wraps_nonzero_exit(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    monkeypatch.setattr(g, "resolve_qwen3_asr_gguf_binary", lambda: "/bin/qwen3-asr-cli")
    monkeypatch.setattr(
        g, "resolve_stt_engine_config",
        lambda _e: {"default_model": "/models/m.gguf", "n_threads": 8, "timeout_seconds": 120.0},
    )
    monkeypatch.setattr(g.os.path, "isfile", lambda p: p == "/models/m.gguf")

    def _boom(argv, **kwargs):
        raise g.subprocess.CalledProcessError(3, argv, output="", stderr="bad audio")

    monkeypatch.setattr(g.subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="exit 3.*bad audio"):
        p._transcribe("/tmp/a.wav", "vi")


@pytest.mark.asyncio
async def test_transcribe_bytes_cleans_up_tempfile(monkeypatch):
    p = g.Qwen3AsrGgufProvider()
    seen = {}

    def _fake_transcribe(self, wav_path, language, model=None):
        seen["wav_path"] = wav_path
        assert g.os.path.isfile(wav_path)  # exists during the call
        return "ok"

    monkeypatch.setattr(g.Qwen3AsrGgufProvider, "_transcribe", _fake_transcribe)
    result = await p.transcribe_bytes(_silent_wav(), "vi")
    assert result.engine == "qwen3_asr_gguf"
    assert result.text == "ok"
    assert result.is_final is True
    assert not g.os.path.isfile(seen["wav_path"])  # cleaned up after


def _tone_wav(sample_rate: int, channels: int, seconds: float = 0.1) -> bytes:
    """A short WAV at an arbitrary rate/channel count (nonzero samples so downmix
    is observable)."""
    n = int(sample_rate * seconds)
    frame = (b"\x11\x22" * channels) * n
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(sample_rate)
    w.writeframes(frame)
    w.close()
    return b.getvalue()


@pytest.mark.parametrize(
    "sample_rate,channels",
    [(48000, 1), (24000, 1), (44100, 2), (16000, 1)],
)
@pytest.mark.asyncio
async def test_transcribe_bytes_normalizes_to_16k_mono(monkeypatch, sample_rate, channels):
    """qwen3-asr-cli needs 16 kHz mono and won't resample -- transcribe_bytes must
    downmix + resample every clip first, or off-rate device/browser audio yields
    empty/garbage transcripts. Assert the WAV that reaches the binary is 16k mono."""
    p = g.Qwen3AsrGgufProvider()
    seen = {}

    def _fake_transcribe(self, wav_path, language, model=None):
        with wave.open(wav_path, "rb") as wf:
            seen["rate"] = wf.getframerate()
            seen["channels"] = wf.getnchannels()
            seen["width"] = wf.getsampwidth()
        return "ok"

    monkeypatch.setattr(g.Qwen3AsrGgufProvider, "_transcribe", _fake_transcribe)
    await p.transcribe_bytes(_tone_wav(sample_rate, channels), "vi")
    assert seen["rate"] == 16000
    assert seen["channels"] == 1
    assert seen["width"] == 2


@pytest.mark.asyncio
async def test_transcribe_bytes_rejects_undecodable_bytes(monkeypatch):
    """Bytes neither wave nor soundfile can parse (real audio garbage, not just
    a WAV with the wrong sample rate) must raise a clear error here rather than
    reach the binary -- qwen3-asr-cli has no container sniffing of its own and
    used to misreport garbage as a nonsense "must be 16kHz" error, which sent
    people chasing the wrong bug (see the scipy-missing incident). Valid mp3/
    ogg/flac now decode fine via wav_bytes_to_pcm16's soundfile fallback and
    never reach this path -- see test_audio.py for that."""
    p = g.Qwen3AsrGgufProvider()

    def _unexpected_transcribe(self, wav_path, language, model=None):
        raise AssertionError("must not reach the binary with undecodable bytes")

    monkeypatch.setattr(g.Qwen3AsrGgufProvider, "_transcribe", _unexpected_transcribe)
    with pytest.raises(RuntimeError, match="could not decode"):
        await p.transcribe_bytes(b"not audio at all", "vi")
