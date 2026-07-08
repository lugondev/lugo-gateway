import pytest
from app.services.conversation.lugo_frame import (
    LUGO_FRAME_OPUS, encode_frame, decode_frame,
)


def test_roundtrip_opus():
    payload = b"\x01\x02\x03\x04"
    frame = encode_frame(LUGO_FRAME_OPUS, payload)
    assert len(frame) == 4 + len(payload)
    ftype, out = decode_frame(frame)
    assert ftype == LUGO_FRAME_OPUS
    assert out == payload


def test_header_layout():
    frame = encode_frame(LUGO_FRAME_OPUS, b"ab")
    assert frame[0] == 0          # type
    assert frame[1] == 0          # reserved
    assert frame[2] == 0 and frame[3] == 2  # payload_size big-endian uint16 == 2


def test_empty_payload_ok():
    ftype, out = decode_frame(encode_frame(LUGO_FRAME_OPUS, b""))
    assert out == b""


def test_decode_rejects_short_header():
    with pytest.raises(ValueError):
        decode_frame(b"\x00\x00")


def test_decode_rejects_size_mismatch():
    # header says 5 bytes but only 2 provided
    with pytest.raises(ValueError):
        decode_frame(b"\x00\x00\x00\x05ab")


def test_encode_rejects_oversized_payload():
    with pytest.raises(ValueError):
        encode_frame(LUGO_FRAME_OPUS, b"x" * 70000)  # > uint16 max
