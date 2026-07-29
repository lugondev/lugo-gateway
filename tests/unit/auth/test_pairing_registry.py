from app.services.auth import pairing as pairing_module
from app.services.auth.pairing import PendingPairingRegistry


def test_create_returns_code_and_poll_token():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    # C3 hardening widened the code from 6 to 8 digits -- see pairing.py's
    # module docstring for why (defense #2, entropy).
    assert len(entry.code) == 8 and entry.code.isdigit()
    assert entry.poll_token
    assert entry.serial == "AA:BB:CC"
    assert entry.claimed is False


def test_get_by_code_and_poll_token():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    assert registry.get_by_code(entry.code) is entry
    assert registry.get_by_poll_token(entry.poll_token) is entry
    assert registry.get_by_code("000000") is None


def test_mark_claimed_sets_fields_and_removes_from_code_lookup():
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    registry.mark_claimed(entry.code, "device-1", "raw-token-abc")
    assert entry.claimed is True
    assert entry.device_id == "device-1"
    assert entry.token == "raw-token-abc"
    # code is single-use: a second claim attempt finds nothing
    assert registry.get_by_code(entry.code) is None
    # but the poll_token lookup (used by the device's status poll) still works
    assert registry.get_by_poll_token(entry.poll_token) is entry


def test_expired_entries_are_swept(monkeypatch):
    monkeypatch.setattr(pairing_module, "_TTL_SECONDS", -1)  # already expired
    registry = PendingPairingRegistry()
    entry = registry.create("AA:BB:CC")
    assert registry.get_by_code(entry.code) is None
    assert registry.get_by_poll_token(entry.poll_token) is None
