"""Lugo v3 binary framing: 4-byte header + payload.

struct { uint8 type; uint8 reserved; uint16 payload_size (big-endian); uint8 payload[]; }
"""
from __future__ import annotations

import struct

LUGO_FRAME_OPUS = 0
LUGO_FRAME_JSON = 1

_HEADER = struct.Struct(">BBH")  # type, reserved, payload_size
_MAX_PAYLOAD = 0xFFFF


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    if len(payload) > _MAX_PAYLOAD:
        raise ValueError(f"payload too large: {len(payload)} > {_MAX_PAYLOAD}")
    return _HEADER.pack(frame_type & 0xFF, 0, len(payload)) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < _HEADER.size:
        raise ValueError("frame shorter than header")
    frame_type, _reserved, size = _HEADER.unpack(data[: _HEADER.size])
    payload = data[_HEADER.size :]
    if len(payload) != size:
        raise ValueError(f"payload size mismatch: header={size} actual={len(payload)}")
    return frame_type, payload
