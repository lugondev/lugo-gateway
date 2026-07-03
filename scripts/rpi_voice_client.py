#!/usr/bin/env python3
"""Reference voice client for Raspberry Pi (or any Python host).

Full-duplex voice loop against the gateway: capture mic → Opus (16 kHz mono) → send;
receive Opus (16 kHz mono) reply frames → play. The heavy STT/LLM/TTS run on the
server; the device only does audio capture/playback + Opus + WebSocket.

Install (Raspberry Pi OS / Debian):
    sudo apt install -y libopus0 portaudio19-dev
    pip install websockets sounddevice opuslib numpy

Run:
    python rpi_voice_client.py --host 192.168.1.50 --port 8000
    # push-to-talk instead of always-on VAD:
    python rpi_voice_client.py --host 192.168.1.50 --ptt
    # activate a saved LLM profile (model/system prompt/TTS/MCP/memory):
    python rpi_voice_client.py --host 192.168.1.50 --profile home-assistant
"""

import argparse
import asyncio
import json
import queue
import sys

import numpy as np
import opuslib
import sounddevice as sd
import websockets

IN_RATE = 16000          # mic / uplink sample rate
OUT_RATE = 16000         # downlink (server output_sample_rate)
FRAME_MS = 60
IN_FRAME = IN_RATE * FRAME_MS // 1000     # 960 samples
OUT_FRAME_MAX = OUT_RATE * 120 // 1000    # decode buffer (max Opus frame)


def build_url(args) -> str:
    q = (
        f"stt_engine={args.stt}&tts_engine={args.tts}&language={args.lang}"
        f"&sample_rate={IN_RATE}&audio_codec=opus"
        f"&output=audio,text&audio_out=opus&output_sample_rate={OUT_RATE}"
    )
    if args.profile:
        # Overrides LLM model/system prompt/TTS/MCP/memory from a saved profile
        # (POST /v1/profiles) — stt/tts/lang above stay as fallbacks.
        q += f"&profile={args.profile}"
    return f"ws://{args.host}:{args.port}/v1/conversation/stream?{q}"


async def main(args) -> None:
    enc = opuslib.Encoder(IN_RATE, 1, opuslib.APPLICATION_VOIP)
    dec = opuslib.Decoder(OUT_RATE, 1)

    mic_q: queue.Queue = queue.Queue()
    speaking = asyncio.Event()  # set while the assistant is playing (half-duplex mute)

    def on_mic(indata, frames, time_info, status):  # sounddevice thread
        mic_q.put(bytes(indata))

    out_stream = sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16")
    in_stream = sd.RawInputStream(
        samplerate=IN_RATE, channels=1, dtype="int16", blocksize=IN_FRAME, callback=on_mic
    )

    async with websockets.connect(build_url(args), max_size=None) as ws:
        hello = json.loads(await ws.recv())
        print("session:", hello.get("stt_engine"), "->", hello.get("llm_model"),
              "->", hello.get("tts_engine"), "| audio_out:", hello.get("audio_out"))

        out_stream.start()
        in_stream.start()

        async def sender():
            loop = asyncio.get_event_loop()
            while True:
                pcm = await loop.run_in_executor(None, mic_q.get)
                # Half-duplex: don't uplink while the assistant is speaking (unless PTT
                # barge-in is desired). PTT mode only sends between space-bar presses.
                if speaking.is_set():
                    continue
                await ws.send(enc.encode(pcm, IN_FRAME))

        async def receiver():
            collecting = False
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    if collecting:
                        pcm = dec.decode(msg, OUT_FRAME_MAX)
                        out_stream.write(pcm)
                    continue
                ev = json.loads(msg)
                kind = ev.get("event")
                if kind == "user_transcript":
                    print("you:", ev.get("text"))
                elif kind == "response_text":
                    print("bot:", ev.get("text"))
                elif kind == "audio_start":
                    collecting = True
                    speaking.set()
                elif kind == "audio_end":
                    collecting = False
                elif kind == "turn_done":
                    speaking.clear()
                elif kind == "aborted":
                    collecting = False
                    speaking.clear()
                elif kind == "error":
                    print("error:", ev.get("message"), file=sys.stderr)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.gather(*tasks)
        finally:
            in_stream.stop()
            out_stream.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", default=8000, type=int)
    p.add_argument("--stt", default="whisper_mlx")
    p.add_argument("--tts", default="vieneu")
    p.add_argument("--lang", default="vi")
    p.add_argument("--profile", default=None, help="named LLM profile (POST /v1/profiles) to activate")
    p.add_argument("--ptt", action="store_true", help="(placeholder) push-to-talk mode")
    asyncio.run(main(p.parse_args()))
