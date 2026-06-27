"""Persistent OmniVoice inference server (runs inside the OmniVoice venv).

The OmniVoice CLI reloads the model on every call (~7s). This sidecar loads the
model ONCE and serves text -> 24 kHz WAV over HTTP, so each request only pays
inference time (RTF ~1), making real-time TTS feasible.

Stdlib + numpy + torch + omnivoice only (no app imports — different venv).
Run:  python omnivoice_sidecar.py --host 127.0.0.1 --port 8762 --model k2-fsa/OmniVoice
"""

import argparse
import io
import json
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock

import numpy as np

_SR = 24000
_MODEL = None
_LOCK = Lock()


def _detect_device(arg: str) -> str:
    if arg:
        return arg
    import torch

    if torch.cuda.is_available():
        return "cuda:0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _to_wav(audio) -> bytes:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(pcm)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/synth":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        kwargs = {"text": req["text"]}
        for key in ("language", "ref_text", "ref_audio", "instruct", "speed"):
            if req.get(key) is not None:
                kwargs[key] = req[key]
        if req.get("class_temperature") is not None:
            kwargs["class_temperature"] = req["class_temperature"]
        try:
            with _LOCK:  # OmniVoice generate is not concurrency-safe
                audios = _MODEL.generate(**kwargs)
            wav = _to_wav(audios[0])
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).encode()[:500]
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


def main() -> None:
    global _MODEL
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8762)
    parser.add_argument("--model", default="k2-fsa/OmniVoice")
    parser.add_argument("--device", default="")
    parser.add_argument("--dtype", default="float16")
    args = parser.parse_args()

    import torch
    from omnivoice import OmniVoice

    dtype = getattr(torch, args.dtype, torch.float16)
    _MODEL = OmniVoice.from_pretrained(args.model, device_map=_detect_device(args.device), dtype=dtype)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
