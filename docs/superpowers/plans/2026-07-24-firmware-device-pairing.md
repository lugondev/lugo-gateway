# Firmware Device Pairing (ESP32 + RPi) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ESP32 and RPi devices obtain a per-device token through the existing server pairing flow (show 6-digit code → user claims in web → device stores its own token), instead of a shared compile-time secret.

**Architecture:** A boot-time pairing state machine on each device, implemented natively (Python on RPi, C on ESP32). No backend or web changes — the server already exposes `/v1/devices/pair/init`, `/v1/devices/pair/status`, and accepts `?device_token=` on `/v1/lugo/stream`. Token resolution priority: explicit override → stored per-device token → run pairing. A **pure disconnect classifier** decides, on every disconnect, whether to reconnect with the same token (network drop) or wipe the token and re-pair (revoke) — this is the UI-critical piece the design calls out.

**Tech Stack:** Python 3.12 + `websockets>=12` + `urllib` (RPi); C11 / ESP-IDF + `esp_http_client` + NVS (ESP32); pytest (RPi host tests); C host-test harness in `esp32-assistant/test/` (ESP32 pure-logic tests).

## Global Constraints

- **No backend or web changes.** Server protocol is complete; touch only `rpi-assistant/` and `esp32-assistant/`.
- **Server pairing endpoints (exact):**
  - `POST /v1/devices/pair/init` body `{"serial": "<str>"}` → `{"success": true, "data": {"code": "<6 digits>", "poll_token": "<str>"}}`
  - `GET /v1/devices/pair/status?poll_token=<str>` → `{"success": true, "data": {"claimed": false}}` OR `{"success": true, "data": {"claimed": true, "device_id": "<str>", "token": "<raw token>"}}`; **HTTP 404** when the session expired (10-min TTL) or poll_token is unknown.
  - WS connect: `/v1/lugo/stream?device_token=<raw token>`.
- **Pairing code TTL is 10 minutes** (server `_TTL_SECONDS = 600`). On HTTP 404 during status polling, re-run `pair/init` and show a fresh code.
- **Poll cadence: 3 seconds.**
- **Revoke signals (exact) — the ONLY two things that mean "wipe token & re-pair":**
  1. WS handshake rejected with **HTTP 401 or 403** (server closes before `accept()` when `resolve_ws_identity` returns None).
  2. Application message `{"type": "goodbye", "reason": "account_disabled"}` received mid-session (server watchdog on revoke).
  - **Everything else is a network drop → keep token, reconnect with backoff:** transport errors, DNS failures, ping timeouts, normal closes, and `{"type":"goodbye","reason":"idle_timeout"}`.
- **Serial (device identity):** ESP32 = eFuse base MAC as lowercase hex `aabbccddeeff`; RPi = contents of `/etc/machine-id` (stable across reflash). A stable serial keeps `find_active_by_serial` recognizing re-paired hardware instead of orphaning rows.
- **Token storage:** ESP32 = NVS namespace `lugo`, key `device_token`; RPi = file (mode `0600`) next to the existing `session_id`.
- **Never log the raw token.** Log the pairing *code* (needed by the user); redact the token.
- **Override / bypass:** if an explicit token is configured (`CONFIG_AA_DEVICE_TOKEN` on ESP32; `server.device_token` in `config.yaml` on RPi), use it verbatim and skip pairing.
- **Out of scope (future work):** GPIO factory-reset button. Both implementations expose a `clear_device_token()` so the button can later call it + reboot — do not implement the button.

---

## File Structure

**RPi (`rpi-assistant/a2a_client/`):**
- Create `device_identity.py` — serial source + token file store (pure/file, no network).
- Create `pairing.py` — HTTP pairing client + polling loop.
- Create `disconnect.py` — pure disconnect classifier.
- Modify `config.py` — add `device_token` (override) and `device_token_path`.
- Modify `ws_protocol.py` — `build_ws_url` appends `?device_token=`.
- Modify `service.py` — boot token resolution, connect-exception handling, `goodbye` handling.
- Tests: `tests/test_device_identity.py`, `tests/test_pairing.py`, `tests/test_disconnect.py`, `tests/test_ws_protocol.py` (extend).

**ESP32 (`esp32-assistant/`):**
- Create component `components/pairing/`:
  - `include/pairing.h`
  - `pairing_logic.c` — pure: serial formatting, disconnect classifier, status-JSON decision (host-testable).
  - `pairing_store.c` — NVS token load/save/clear (on-device).
  - `pairing_net.c` — `esp_http_client` calls + polling loop (on-device).
- Modify `main/main.c` — resolve-or-pair before `ws_client_start`; handle revoke signals.
- Modify `components/ws_client/ws_client.c` + `include/ws_client.h` — surface handshake HTTP status and goodbye reason to `main`.
- Modify `main/Kconfig.projbuild` — clarify `CONFIG_AA_DEVICE_TOKEN` is an override.
- Tests: `test/test_pairing_logic.c` + `test/Makefile` target.

---

# Part A — RPi (reference implementation)

Run all RPi commands from `rpi-assistant/`. Tests: `python -m pytest tests/ -q`.

### Task R1: Device identity & token file store

**Files:**
- Create: `rpi-assistant/a2a_client/device_identity.py`
- Test: `rpi-assistant/tests/test_device_identity.py`

**Interfaces:**
- Produces:
  - `read_device_serial(machine_id_path: str = "/etc/machine-id") -> str` — returns stripped machine-id; raises `RuntimeError` if missing/blank.
  - `load_device_token(path: str) -> str | None` — token text or None if missing/blank.
  - `save_device_token(path: str, token: str) -> None` — writes token, `parents=True`, chmod `0o600`.
  - `clear_device_token(path: str) -> None` — removes the file if present (the future factory-reset hook).

- [ ] **Step 1: Write the failing test**

```python
# rpi-assistant/tests/test_device_identity.py
import os
import pytest
from a2a_client.device_identity import (
    read_device_serial, load_device_token, save_device_token, clear_device_token,
)


def test_read_serial_from_machine_id(tmp_path):
    p = tmp_path / "machine-id"
    p.write_text("abc123\n", encoding="utf-8")
    assert read_device_serial(str(p)) == "abc123"


def test_read_serial_missing_raises(tmp_path):
    with pytest.raises(RuntimeError):
        read_device_serial(str(tmp_path / "nope"))


def test_token_roundtrip_and_perms(tmp_path):
    path = str(tmp_path / "sub" / "device_token")
    assert load_device_token(path) is None
    save_device_token(path, "tok-xyz")
    assert load_device_token(path) == "tok-xyz"
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_clear_token(tmp_path):
    path = str(tmp_path / "device_token")
    save_device_token(path, "tok")
    clear_device_token(path)
    assert load_device_token(path) is None
    clear_device_token(path)  # idempotent, no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_device_identity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a_client.device_identity'`

- [ ] **Step 3: Write minimal implementation**

```python
# rpi-assistant/a2a_client/device_identity.py
from __future__ import annotations

import os
from pathlib import Path


def read_device_serial(machine_id_path: str = "/etc/machine-id") -> str:
    p = Path(machine_id_path)
    if not p.is_file():
        raise RuntimeError(f"machine-id not found at {machine_id_path}")
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"machine-id at {machine_id_path} is empty")
    return value


def load_device_token(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    value = p.read_text(encoding="utf-8").strip()
    return value or None


def save_device_token(path: str, token: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    os.chmod(p, 0o600)


def clear_device_token(path: str) -> None:
    Path(path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_device_identity.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add a2a_client/device_identity.py tests/test_device_identity.py
git commit -m "feat(rpi): device serial + token file store"
```

---

### Task R2: Pairing HTTP client & polling loop

**Files:**
- Create: `rpi-assistant/a2a_client/pairing.py`
- Test: `rpi-assistant/tests/test_pairing.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (network only).
- Produces:
  - `pair_init(base_url: str, serial: str, *, opener=urllib.request.urlopen) -> tuple[str, str]` — returns `(code, poll_token)`.
  - `pair_status(base_url: str, poll_token: str, *, opener=urllib.request.urlopen) -> dict | None` — returns the `data` dict, or `None` on HTTP 404 (expired/unknown session).
  - `run_pairing(base_url: str, serial: str, *, show_code, sleep, opener=urllib.request.urlopen, poll_interval: float = 3.0) -> str` — runs init → show code → poll until claimed, re-initing on 404; returns the raw device token. `show_code(code: str)` is a callback (OLED + log); `sleep(seconds: float)` is injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
# rpi-assistant/tests/test_pairing.py
import io
import json
import urllib.error
import pytest
from a2a_client import pairing


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _json_resp(payload):
    return FakeResp(json.dumps(payload).encode("utf-8"))


def test_pair_init_returns_code_and_token():
    def opener(req, timeout=0):
        assert req.full_url.endswith("/v1/devices/pair/init")
        assert json.loads(req.data) == {"serial": "srl"}
        return _json_resp({"success": True, "data": {"code": "123456", "poll_token": "pt"}})
    assert pairing.pair_init("http://h:8000", "srl", opener=opener) == ("123456", "pt")


def test_pair_status_404_returns_none():
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)
    assert pairing.pair_status("http://h:8000", "pt", opener=opener) is None


def test_run_pairing_shows_code_polls_then_returns_token():
    shown = []
    calls = {"n": 0}

    def opener(req, timeout=0):
        if req.full_url.endswith("/pair/init"):
            return _json_resp({"success": True, "data": {"code": "999000", "poll_token": "pt"}})
        calls["n"] += 1
        if calls["n"] < 2:
            return _json_resp({"success": True, "data": {"claimed": False}})
        return _json_resp({"success": True, "data": {"claimed": True, "device_id": "d1", "token": "TOK"}})

    token = pairing.run_pairing(
        "http://h:8000", "srl",
        show_code=shown.append, sleep=lambda s: None, opener=opener, poll_interval=0,
    )
    assert token == "TOK"
    assert shown == ["999000"]


def test_run_pairing_reinits_on_expiry():
    shown = []
    seq = iter([
        ("init", {"code": "111111", "poll_token": "p1"}),
        ("404", None),                                   # p1 expired
        ("init", {"code": "222222", "poll_token": "p2"}),
        ("claimed", {"claimed": True, "device_id": "d", "token": "TOK2"}),
    ])
    state = {"cur": None}

    def opener(req, timeout=0):
        import urllib.error
        kind, data = next(seq)
        if kind == "init":
            return _json_resp({"success": True, "data": data})
        if kind == "404":
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)
        return _json_resp({"success": True, "data": data})

    token = pairing.run_pairing(
        "http://h:8000", "srl",
        show_code=shown.append, sleep=lambda s: None, opener=opener, poll_interval=0,
    )
    assert token == "TOK2"
    assert shown == ["111111", "222222"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pairing.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a_client.pairing'`

- [ ] **Step 3: Write minimal implementation**

```python
# rpi-assistant/a2a_client/pairing.py
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


def _post_json(base_url: str, path: str, body: dict, opener) -> dict:
    req = urllib.request.Request(
        urllib.parse.urljoin(base_url, path),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(req, timeout=30) as resp:  # nosec B310
        return json.loads(resp.read())


def pair_init(base_url: str, serial: str, *, opener=urllib.request.urlopen) -> tuple[str, str]:
    data = _post_json(base_url, "/v1/devices/pair/init", {"serial": serial}, opener)["data"]
    return data["code"], data["poll_token"]


def pair_status(base_url: str, poll_token: str, *, opener=urllib.request.urlopen) -> dict | None:
    url = urllib.parse.urljoin(
        base_url, "/v1/devices/pair/status?" + urllib.parse.urlencode({"poll_token": poll_token})
    )
    req = urllib.request.Request(url, method="GET")
    try:
        with opener(req, timeout=30) as resp:  # nosec B310
            return json.loads(resp.read())["data"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def run_pairing(
    base_url: str,
    serial: str,
    *,
    show_code: Callable[[str], None],
    sleep: Callable[[float], None],
    opener=urllib.request.urlopen,
    poll_interval: float = 3.0,
) -> str:
    while True:
        code, poll_token = pair_init(base_url, serial, opener=opener)
        show_code(code)
        while True:
            status = pair_status(base_url, poll_token, opener=opener)
            if status is None:
                break  # expired -> re-init, show a fresh code
            if status.get("claimed"):
                return status["token"]
            sleep(poll_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pairing.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add a2a_client/pairing.py tests/test_pairing.py
git commit -m "feat(rpi): pairing HTTP client + polling loop"
```

---

### Task R3: Disconnect classifier (UI-critical)

**Files:**
- Create: `rpi-assistant/a2a_client/disconnect.py`
- Test: `rpi-assistant/tests/test_disconnect.py`

**Interfaces:**
- Produces:
  - `RECONNECT = "reconnect"`, `REPAIR = "repair"` (module constants).
  - `classify_disconnect(handshake_status: int | None, goodbye_reason: str | None) -> str` — pure. Returns `REPAIR` iff the disconnect was an auth rejection (`handshake_status in (401, 403)`) or a revoke goodbye (`goodbye_reason == "account_disabled"`); otherwise `RECONNECT`.

This is the single source of truth for "network drop vs revoke". Every disconnect path in `service.py` funnels through it, so the token is wiped **only** on the two revoke signals and never on an ordinary drop.

- [ ] **Step 1: Write the failing test**

```python
# rpi-assistant/tests/test_disconnect.py
from a2a_client.disconnect import classify_disconnect, RECONNECT, REPAIR


def test_handshake_403_is_repair():
    assert classify_disconnect(403, None) == REPAIR


def test_handshake_401_is_repair():
    assert classify_disconnect(401, None) == REPAIR


def test_goodbye_account_disabled_is_repair():
    assert classify_disconnect(None, "account_disabled") == REPAIR


def test_idle_timeout_goodbye_is_reconnect():
    assert classify_disconnect(None, "idle_timeout") == RECONNECT


def test_plain_network_drop_is_reconnect():
    assert classify_disconnect(None, None) == RECONNECT


def test_server_5xx_handshake_is_reconnect():
    # a 500 during handshake is an outage, not a revoke — keep the token
    assert classify_disconnect(500, None) == RECONNECT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_disconnect.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'a2a_client.disconnect'`

- [ ] **Step 3: Write minimal implementation**

```python
# rpi-assistant/a2a_client/disconnect.py
from __future__ import annotations

RECONNECT = "reconnect"
REPAIR = "repair"

_AUTH_REJECT_STATUS = (401, 403)


def classify_disconnect(handshake_status: int | None, goodbye_reason: str | None) -> str:
    """Decide what a disconnect means. REPAIR (wipe token, re-pair) only on the
    two revoke signals; everything else is a recoverable network drop."""
    if handshake_status in _AUTH_REJECT_STATUS:
        return REPAIR
    if goodbye_reason == "account_disabled":
        return REPAIR
    return RECONNECT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_disconnect.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add a2a_client/disconnect.py tests/test_disconnect.py
git commit -m "feat(rpi): disconnect classifier (network-drop vs revoke)"
```

---

### Task R4: Config — device token override + path

**Files:**
- Modify: `rpi-assistant/a2a_client/config.py`
- Modify: `rpi-assistant/config.example.yaml`
- Test: `rpi-assistant/tests/test_config_device_token.py` (create)

**Interfaces:**
- Produces (new `Config` fields): `device_token: str | None`, `device_token_path: str`.

- [ ] **Step 1: Write the failing test**

```python
# rpi-assistant/tests/test_config_device_token.py
from a2a_client.config import load_config


def test_device_token_defaults(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("server:\n  host: h\n", encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg.device_token is None
    assert cfg.device_token_path.endswith("device_token")


def test_device_token_override(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("server:\n  host: h\n  device_token: DEVTOK\n", encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert cfg.device_token == "DEVTOK"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_device_token.py -q`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'device_token'`

- [ ] **Step 3: Write minimal implementation**

In `config.py`, add two fields to the `Config` dataclass (after `session_state_path`):

```python
    session_state_path: str
    device_token: str | None
    device_token_path: str
```

In `load_config`, add near the top of the return (after `server = raw.get("server", {})` already exists) two computed values and pass them into `Config(...)`:

```python
    # inside load_config, before the return:
    _default_token_path = os.path.join(
        os.path.dirname(
            os.path.expanduser(
                str(session.get("session_state_path", _DEFAULT_SESSION_STATE_PATH))
            )
        ),
        "device_token",
    )
```

Then add to the `Config(...)` call:

```python
        device_token=server.get("device_token"),
        device_token_path=os.path.expanduser(
            str(server.get("device_token_path", _default_token_path))
        ),
```

In `config.example.yaml`, under `server:` add documented, commented-out keys:

```yaml
server:
  host: 127.0.0.1
  port: 8000
  secure: false
  # device_token: ""          # dev/legacy override; if set, skips pairing
  # device_token_path: ""     # defaults next to session_state_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_device_token.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add a2a_client/config.py config.example.yaml tests/test_config_device_token.py
git commit -m "feat(rpi): config device_token override + token path"
```

---

### Task R5: `build_ws_url` sends `?device_token=`

**Files:**
- Modify: `rpi-assistant/a2a_client/ws_protocol.py`
- Test: `rpi-assistant/tests/test_ws_protocol.py` (extend)

**Interfaces:**
- Consumes: `Config.device_token` is not used here; the token is passed explicitly (the service holds the *resolved* token, which may come from the file, not just config).
- Produces: `build_ws_url(config: Config, device_token: str | None = None) -> str` — appends `?device_token=<token>` when a non-empty token is given; unchanged URL otherwise.

- [ ] **Step 1: Write the failing test**

```python
# add to rpi-assistant/tests/test_ws_protocol.py
from a2a_client.ws_protocol import build_ws_url


class _Cfg:
    host = "h"; port = 8000; secure = False


def test_build_ws_url_without_token():
    assert build_ws_url(_Cfg()) == "ws://h:8000/v1/lugo/stream"


def test_build_ws_url_with_token():
    assert build_ws_url(_Cfg(), "TOK") == "ws://h:8000/v1/lugo/stream?device_token=TOK"


def test_build_ws_url_empty_token_omitted():
    assert build_ws_url(_Cfg(), "") == "ws://h:8000/v1/lugo/stream"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ws_protocol.py -q`
Expected: FAIL — `build_ws_url() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Write minimal implementation**

Replace `build_ws_url` in `ws_protocol.py`:

```python
import urllib.parse


def build_ws_url(config: Config, device_token: str | None = None) -> str:
    scheme = "wss" if config.secure else "ws"
    url = f"{scheme}://{config.host}:{config.port}/v1/lugo/stream"
    if device_token:
        url += "?" + urllib.parse.urlencode({"device_token": device_token})
    return url
```

(`Config` is already imported in this module; if not, `from .config import Config`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ws_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add a2a_client/ws_protocol.py tests/test_ws_protocol.py
git commit -m "feat(rpi): build_ws_url injects device_token"
```

---

### Task R6: Wire pairing + revoke handling into the service

**Files:**
- Modify: `rpi-assistant/a2a_client/service.py`
- Test: `rpi-assistant/tests/test_service_pairing.py` (create — tests the extracted helpers, not the full asyncio loop)

**Interfaces:**
- Consumes: `device_identity.*`, `pairing.run_pairing`, `disconnect.classify_disconnect` / `REPAIR` / `RECONNECT`, `build_ws_url(config, token)`.
- Produces (methods on `AudioToAudioService`, extracted so they are unit-testable):
  - `resolve_device_token(self) -> str` — returns override, else stored token, else runs pairing and persists the result.
  - `on_disconnect(self, handshake_status: int | None, goodbye_reason: str | None) -> str` — classifies; on `REPAIR` calls `clear_device_token(...)` and drops the in-memory token so the next loop re-pairs; returns the action.

The connect loop change: build the URL with `self._device_token`; catch `websockets.exceptions.InvalidStatus` to capture the handshake status; track the last `goodbye` reason seen by the receiver; after each connection ends, call `on_disconnect(...)`.

- [ ] **Step 1: Write the failing test**

```python
# rpi-assistant/tests/test_service_pairing.py
from a2a_client.disconnect import REPAIR, RECONNECT


class FakeService:
    """Mirrors the two extracted helpers so we can test them without asyncio/audio."""
    def __init__(self, token_path, override=None, stored=None):
        from a2a_client import device_identity
        self._di = device_identity
        self.token_path = token_path
        self.override = override
        self._device_token = None
        if stored:
            device_identity.save_device_token(token_path, stored)

    # copies of the real logic (kept in sync with service.py)
    def resolve_device_token(self, run_pairing):
        if self.override:
            self._device_token = self.override
            return self._device_token
        tok = self._di.load_device_token(self.token_path)
        if tok:
            self._device_token = tok
            return tok
        tok = run_pairing()
        self._di.save_device_token(self.token_path, tok)
        self._device_token = tok
        return tok

    def on_disconnect(self, handshake_status, goodbye_reason):
        from a2a_client.disconnect import classify_disconnect, REPAIR
        action = classify_disconnect(handshake_status, goodbye_reason)
        if action == REPAIR:
            self._di.clear_device_token(self.token_path)
            self._device_token = None
        return action


def test_resolve_uses_override(tmp_path):
    s = FakeService(str(tmp_path / "t"), override="OVR")
    assert s.resolve_device_token(run_pairing=lambda: "PAIRED") == "OVR"


def test_resolve_uses_stored(tmp_path):
    s = FakeService(str(tmp_path / "t"), stored="STORED")
    assert s.resolve_device_token(run_pairing=lambda: "PAIRED") == "STORED"


def test_resolve_pairs_and_persists(tmp_path):
    from a2a_client import device_identity
    path = str(tmp_path / "t")
    s = FakeService(path)
    assert s.resolve_device_token(run_pairing=lambda: "PAIRED") == "PAIRED"
    assert device_identity.load_device_token(path) == "PAIRED"


def test_on_disconnect_revoke_wipes_token(tmp_path):
    path = str(tmp_path / "t")
    s = FakeService(path, stored="STORED")
    s.resolve_device_token(run_pairing=lambda: "x")
    assert s.on_disconnect(403, None) == REPAIR
    from a2a_client import device_identity
    assert device_identity.load_device_token(path) is None
    assert s._device_token is None


def test_on_disconnect_network_drop_keeps_token(tmp_path):
    path = str(tmp_path / "t")
    s = FakeService(path, stored="STORED")
    s.resolve_device_token(run_pairing=lambda: "x")
    assert s.on_disconnect(None, None) == RECONNECT
    from a2a_client import device_identity
    assert device_identity.load_device_token(path) == "STORED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_service_pairing.py -q`
Expected: FAIL with `ModuleNotFoundError` / import error until the modules from R1–R3 exist (they do after R1–R3). If R1–R3 are done, this test passes on its own copies — run it to confirm GREEN, then port the identical logic into `service.py` in Step 3 and keep this test as the guard.

> Note: this test intentionally holds copies of the two helpers so it stays fast and asyncio-free. Step 3 puts the **same** logic on the real class; the guard is that both read identically. Keep them in sync.

- [ ] **Step 3: Wire the real service**

In `service.py`:

1. Add imports:

```python
from .device_identity import read_device_serial, load_device_token, save_device_token, clear_device_token
from .pairing import run_pairing
from .disconnect import classify_disconnect, REPAIR
```

2. In `__init__`, initialize token + last-goodbye holder:

```python
        self._device_token: str | None = None
        self._last_goodbye_reason: str | None = None
```

3. Add the two helpers (logic identical to the test's copies):

```python
    def resolve_device_token(self) -> str:
        if self.config.device_token:
            self._device_token = self.config.device_token
            return self._device_token
        tok = load_device_token(self.config.device_token_path)
        if tok:
            self._device_token = tok
            return tok
        serial = read_device_serial()
        base = f"{'https' if self.config.secure else 'http'}://{self.config.host}:{self.config.port}"

        def _show(code: str) -> None:
            self.log(f"pairing code: {code}")   # token never logged; code is safe
            self.oled.show("Pair code", code)

        tok = run_pairing(base, serial, show_code=_show, sleep=time.sleep)
        save_device_token(self.config.device_token_path, tok)
        self._device_token = tok
        return tok

    def on_disconnect(self, handshake_status: int | None, goodbye_reason: str | None) -> str:
        action = classify_disconnect(handshake_status, goodbye_reason)
        if action == REPAIR:
            clear_device_token(self.config.device_token_path)
            self._device_token = None
            self.log("device token revoked by server -- will re-pair")
            self.oled.show("Unpaired", "re-pairing")
        return action
```

4. In the receiver's `goodbye` branch (around line 366), capture the reason:

```python
            elif name == "goodbye":
                self._last_goodbye_reason = event.get("reason")
                self.log(f"server goodbye: {event.get('reason', '')}")
```

5. In the connect loop (`run`/`_run` around line 390), before the loop resolve the token once; inside, build the URL with it, capture handshake status, and call `on_disconnect` after each connection:

```python
        # before the while loop:
        self.resolve_device_token()

        while not self.stop_event.is_set():
            self._session_ready.clear()
            self._last_goodbye_reason = None
            handshake_status: int | None = None
            self.leds.connecting()
            self.oled.connecting()
            ws_url = build_ws_url(self.config, self._device_token)
            try:
                async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                    # ... unchanged connected body ...
                    ...
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as exc:
                handshake_status = exc.response.status_code
                self.log(f"handshake rejected: {handshake_status}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"connection error: {exc}")
                self.leds.error()
                self.oled.error("WS ERR")
            finally:
                self._stop_warming_reminder()

            if self.stop_event.is_set():
                break

            action = self.on_disconnect(handshake_status, self._last_goodbye_reason)
            if action == REPAIR:
                self.resolve_device_token()   # blocks on the pairing flow, shows fresh code
                backoff = self.config.reconnect_initial_seconds
                continue

            self.log(f"reconnect in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.config.reconnect_max_seconds)
```

> `websockets.exceptions.InvalidStatus` (websockets ≥12) exposes `.response.status_code`. Keep the `except InvalidStatus` clause **above** the generic `except Exception`, so an auth reject is classified as a handshake status rather than a plain drop.

- [ ] **Step 4: Run the full RPi suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (all, including the new files)

- [ ] **Step 5: Commit**

```bash
git add a2a_client/service.py tests/test_service_pairing.py
git commit -m "feat(rpi): pair on boot + wipe token on revoke, keep on drop"
```

---

# Part B — ESP32

Run all ESP32 host tests from `esp32-assistant/test/`: `make test`.

### Task E1: Pure pairing logic — serial, classifier, status parse (host-tested)

**Files:**
- Create: `esp32-assistant/components/pairing/include/pairing.h`
- Create: `esp32-assistant/components/pairing/pairing_logic.c`
- Create: `esp32-assistant/test/test_pairing_logic.c`
- Modify: `esp32-assistant/test/Makefile`

**Interfaces:**
- Produces (pure, no ESP-IDF deps — host-compilable):
  - `void aa_format_serial(const uint8_t mac[6], char out[13]);` — writes 12 lowercase hex chars + NUL.
  - `typedef enum { AA_DISCONNECT_RECONNECT = 0, AA_DISCONNECT_REPAIR = 1 } aa_disconnect_t;`
  - `aa_disconnect_t aa_classify_disconnect(int handshake_status, const char *goodbye_reason);` — `REPAIR` iff `handshake_status` is 401/403 or `goodbye_reason` equals `"account_disabled"`; pass `handshake_status = 0` when the connection was established (not a handshake reject) and `goodbye_reason = NULL` when none.
  - `int aa_parse_pair_status(const char *json, char *token_out, int token_cap);` — returns `1` and fills `token_out` when `"claimed":true` with a token; `0` when not yet claimed; `-1` on parse failure. (Minimal hand parse to stay dependency-free and host-testable — the field order from the server is fixed.)

- [ ] **Step 1: Write the failing test**

```c
// esp32-assistant/test/test_pairing_logic.c
#include "pairing.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void test_format_serial(void) {
    uint8_t mac[6] = {0xAA, 0xBB, 0xCC, 0x00, 0x1F, 0xE9};
    char out[13];
    aa_format_serial(mac, out);
    CHECK(strcmp(out, "aabbcc001fe9") == 0);
}

static void test_classify(void) {
    CHECK(aa_classify_disconnect(403, NULL) == AA_DISCONNECT_REPAIR);
    CHECK(aa_classify_disconnect(401, NULL) == AA_DISCONNECT_REPAIR);
    CHECK(aa_classify_disconnect(0, "account_disabled") == AA_DISCONNECT_REPAIR);
    CHECK(aa_classify_disconnect(0, "idle_timeout") == AA_DISCONNECT_RECONNECT);
    CHECK(aa_classify_disconnect(0, NULL) == AA_DISCONNECT_RECONNECT);
    CHECK(aa_classify_disconnect(500, NULL) == AA_DISCONNECT_RECONNECT);
}

static void test_parse_status(void) {
    char tok[64];
    CHECK(aa_parse_pair_status("{\"success\":true,\"data\":{\"claimed\":false}}", tok, sizeof tok) == 0);
    int r = aa_parse_pair_status(
        "{\"success\":true,\"data\":{\"claimed\":true,\"device_id\":\"d\",\"token\":\"TOK123\"}}",
        tok, sizeof tok);
    CHECK(r == 1);
    CHECK(strcmp(tok, "TOK123") == 0);
    CHECK(aa_parse_pair_status("not json", tok, sizeof tok) == -1);
}

int main(void) {
    test_format_serial();
    test_classify();
    test_parse_status();
    if (failures) { printf("%d FAILURES\n", failures); return 1; }
    printf("OK\n");
    return 0;
}
```

- [ ] **Step 2: Add the Makefile target and run to verify it fails**

Add to `esp32-assistant/test/Makefile`:

```make
PAIRING_CFLAGS = -std=c11 -Wall -Wextra -g -O0 -I../components/pairing/include
SRC_PAIRING_LOGIC = ../components/pairing/pairing_logic.c

test_pairing_logic: test_pairing_logic.c $(SRC_PAIRING_LOGIC)
	$(CC) $(PAIRING_CFLAGS) -o $@ $^
```

And append `test_pairing_logic` to both the `.PHONY: test` dependency list and the run block.

Run: `make test_pairing_logic`
Expected: FAIL — `pairing.h: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

```c
// esp32-assistant/components/pairing/include/pairing.h
#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void aa_format_serial(const uint8_t mac[6], char out[13]);

typedef enum { AA_DISCONNECT_RECONNECT = 0, AA_DISCONNECT_REPAIR = 1 } aa_disconnect_t;

aa_disconnect_t aa_classify_disconnect(int handshake_status, const char *goodbye_reason);

int aa_parse_pair_status(const char *json, char *token_out, int token_cap);

#ifdef __cplusplus
}
#endif
```

```c
// esp32-assistant/components/pairing/pairing_logic.c
#include "pairing.h"
#include <string.h>
#include <stdio.h>

void aa_format_serial(const uint8_t mac[6], char out[13]) {
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 6; i++) {
        out[i * 2]     = hex[(mac[i] >> 4) & 0xF];
        out[i * 2 + 1] = hex[mac[i] & 0xF];
    }
    out[12] = '\0';
}

aa_disconnect_t aa_classify_disconnect(int handshake_status, const char *goodbye_reason) {
    if (handshake_status == 401 || handshake_status == 403)
        return AA_DISCONNECT_REPAIR;
    if (goodbye_reason && strcmp(goodbye_reason, "account_disabled") == 0)
        return AA_DISCONNECT_REPAIR;
    return AA_DISCONNECT_RECONNECT;
}

int aa_parse_pair_status(const char *json, char *token_out, int token_cap) {
    if (!json || !strstr(json, "\"data\"")) return -1;
    if (strstr(json, "\"claimed\":true") == NULL) {
        // explicitly not claimed only if we can see claimed:false; else parse error
        return strstr(json, "\"claimed\":false") ? 0 : -1;
    }
    const char *t = strstr(json, "\"token\":\"");
    if (!t) return -1;
    t += strlen("\"token\":\"");
    int i = 0;
    while (t[i] && t[i] != '"' && i < token_cap - 1) { token_out[i] = t[i]; i++; }
    if (t[i] != '"') return -1;
    token_out[i] = '\0';
    return 1;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test_pairing_logic && ./test_pairing_logic`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add components/pairing/include/pairing.h components/pairing/pairing_logic.c test/test_pairing_logic.c test/Makefile
git commit -m "feat(esp32): pure pairing logic (serial/classifier/status parse) + host tests"
```

---

### Task E2: NVS token store (on-device)

**Files:**
- Create: `esp32-assistant/components/pairing/pairing_store.c`
- Modify: `esp32-assistant/components/pairing/include/pairing.h`
- Modify: `esp32-assistant/components/pairing/CMakeLists.txt` (create)

**Interfaces:**
- Produces:
  - `int aa_load_device_token(char *out, int cap);` — returns length (>0) if found, `0` if absent, `<0` on NVS error.
  - `int aa_save_device_token(const char *token);` — `0` on success.
  - `int aa_clear_device_token(void);` — `0` on success (idempotent). **The future factory-reset hook.**

No host test (NVS is device-only); verified on-device in E5.

- [ ] **Step 1: Add declarations to `pairing.h`**

```c
int aa_load_device_token(char *out, int cap);
int aa_save_device_token(const char *token);
int aa_clear_device_token(void);
```

- [ ] **Step 2: Create the component build file**

```cmake
# esp32-assistant/components/pairing/CMakeLists.txt
idf_component_register(
    SRCS "pairing_logic.c" "pairing_store.c" "pairing_net.c"
    INCLUDE_DIRS "include"
    REQUIRES nvs_flash esp_http_client display
)
```

> `pairing_net.c` is added in E3; create it as an empty stub now (`#include "pairing.h"`) so this build file is valid, or add E3 before building on-device.

- [ ] **Step 3: Write implementation**

```c
// esp32-assistant/components/pairing/pairing_store.c
#include "pairing.h"
#include "nvs.h"
#include "nvs_flash.h"
#include <string.h>

#define NS  "lugo"
#define KEY "device_token"

int aa_load_device_token(char *out, int cap) {
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return 0;
    size_t len = (size_t) cap;
    esp_err_t err = nvs_get_str(h, KEY, out, &len);
    nvs_close(h);
    if (err == ESP_OK) return (int) strlen(out);
    if (err == ESP_ERR_NVS_NOT_FOUND) return 0;
    return -1;
}

int aa_save_device_token(const char *token) {
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_str(h, KEY, token);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK ? 0 : -1;
}

int aa_clear_device_token(void) {
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_erase_key(h, KEY);
    if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;  // idempotent
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK ? 0 : -1;
}
```

- [ ] **Step 4: Verify host tests still build**

Run: `cd esp32-assistant/test && make test_pairing_logic && ./test_pairing_logic`
Expected: `OK` (store code is not part of the host test target).

- [ ] **Step 5: Commit**

```bash
git add components/pairing/pairing_store.c components/pairing/include/pairing.h components/pairing/CMakeLists.txt
git commit -m "feat(esp32): NVS device-token store (load/save/clear)"
```

---

### Task E3: Pairing HTTP client + poll loop (on-device)

**Files:**
- Create: `esp32-assistant/components/pairing/pairing_net.c`
- Modify: `esp32-assistant/components/pairing/include/pairing.h`

**Interfaces:**
- Consumes: `aa_parse_pair_status`, `aa_save_device_token`, `aa_format_serial`.
- Produces:
  - `typedef void (*aa_show_code_fn)(const char *code);`
  - `int aa_run_pairing(const char *base_url, const char *serial, aa_show_code_fn show, char *token_out, int token_cap);` — init → show code → poll every 3 s, re-init on HTTP 404, return `0` and fill `token_out` on claim; `<0` on unrecoverable error.

Uses `esp_http_client`. Parses `pair/init` response for `code` and `poll_token` with the same minimal string scan style as `aa_parse_pair_status` (add a small `aa_parse_pair_init` static helper, or reuse a generic field extractor). Poll delay via `vTaskDelay(pdMS_TO_TICKS(3000))`.

- [ ] **Step 1: Add declarations to `pairing.h`**

```c
typedef void (*aa_show_code_fn)(const char *code);
int aa_run_pairing(const char *base_url, const char *serial,
                   aa_show_code_fn show, char *token_out, int token_cap);
```

- [ ] **Step 2: Write implementation**

```c
// esp32-assistant/components/pairing/pairing_net.c
#include "pairing.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include <stdio.h>

static const char *TAG = "pairing";

// Extract a JSON string field value: finds "\"key\":\"" and copies until the next quote.
static int extract_str(const char *json, const char *key, char *out, int cap) {
    char pat[48];
    snprintf(pat, sizeof pat, "\"%s\":\"", key);
    const char *p = strstr(json, pat);
    if (!p) return -1;
    p += strlen(pat);
    int i = 0;
    while (p[i] && p[i] != '"' && i < cap - 1) { out[i] = p[i]; i++; }
    if (p[i] != '"') return -1;
    out[i] = '\0';
    return i;
}

// Minimal blocking GET/POST into a fixed buffer. Returns HTTP status, or <0 on transport error.
static int http_call(const char *url, const char *method, const char *body,
                     char *resp, int resp_cap) {
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 30000 };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_method(c, strcmp(method, "POST") == 0
                                  ? HTTP_METHOD_POST : HTTP_METHOD_GET);
    if (body) {
        esp_http_client_set_header(c, "Content-Type", "application/json");
        esp_http_client_set_post_field(c, body, strlen(body));
    }
    int status = -1;
    if (esp_http_client_open(c, body ? (int) strlen(body) : 0) == ESP_OK) {
        if (body) esp_http_client_write(c, body, strlen(body));
        esp_http_client_fetch_headers(c);
        status = esp_http_client_get_status_code(c);
        int n = esp_http_client_read_response(c, resp, resp_cap - 1);
        resp[n > 0 ? n : 0] = '\0';
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return status;
}

int aa_run_pairing(const char *base_url, const char *serial,
                   aa_show_code_fn show, char *token_out, int token_cap) {
    char url[256], body[96], resp[512], code[8], poll_token[128];
    for (;;) {
        snprintf(url, sizeof url, "%s/v1/devices/pair/init", base_url);
        snprintf(body, sizeof body, "{\"serial\":\"%s\"}", serial);
        int st = http_call(url, "POST", body, resp, sizeof resp);
        if (st != 200) { ESP_LOGW(TAG, "pair/init http %d", st); vTaskDelay(pdMS_TO_TICKS(3000)); continue; }
        if (extract_str(resp, "code", code, sizeof code) < 0 ||
            extract_str(resp, "poll_token", poll_token, sizeof poll_token) < 0) {
            ESP_LOGW(TAG, "pair/init parse failed"); vTaskDelay(pdMS_TO_TICKS(3000)); continue;
        }
        ESP_LOGI(TAG, "pairing code: %s", code);   // code is safe to log; token is not
        if (show) show(code);

        for (;;) {
            snprintf(url, sizeof url, "%s/v1/devices/pair/status?poll_token=%s", base_url, poll_token);
            st = http_call(url, "GET", NULL, resp, sizeof resp);
            if (st == 404) break;                    // expired -> re-init, fresh code
            if (st == 200) {
                int r = aa_parse_pair_status(resp, token_out, token_cap);
                if (r == 1) { aa_save_device_token(token_out); return 0; }
            }
            vTaskDelay(pdMS_TO_TICKS(3000));
        }
    }
}
```

- [ ] **Step 3: Verify host tests unaffected**

Run: `cd esp32-assistant/test && make test_pairing_logic && ./test_pairing_logic`
Expected: `OK`

> `pairing_net.c` is not in the host test target; it is compiled by the ESP-IDF build. Full verification is on-device (E5).

- [ ] **Step 4: Commit**

```bash
git add components/pairing/pairing_net.c components/pairing/include/pairing.h
git commit -m "feat(esp32): pairing HTTP client + poll loop"
```

---

### Task E4: Surface handshake status + goodbye reason from `ws_client`

**Files:**
- Modify: `esp32-assistant/components/ws_client/ws_client.c`
- Modify: `esp32-assistant/components/ws_client/include/ws_client.h`

**Interfaces:**
- Produces: a way for `main.c` to learn, after a WS session ends, (a) the HTTP handshake status if the handshake was rejected (from `WEBSOCKET_EVENT_ERROR` / the transport's `esp_transport_get_errno` / HTTP status on the event), and (b) the last `goodbye` reason. Concretely, extend the existing event callback contract: add an out-param or a getter `int ws_client_last_handshake_status(void)` and reuse the existing GOODBYE parsing in `main.c` to record the reason.

> The exact wiring depends on the current `on_event` signature. Keep the change minimal: store the handshake status in a static when `WEBSOCKET_EVENT_ERROR` carries an HTTP status, expose it via a getter, and reset it on `WEBSOCKET_EVENT_CONNECTED`.

- [ ] **Step 1: Read the current event handling**

Run: `sed -n '30,90p' esp32-assistant/components/ws_client/ws_client.c`
Identify the `WEBSOCKET_EVENT_ERROR` and `WEBSOCKET_EVENT_DISCONNECTED` cases and whether an HTTP status is available on the event data (`esp_websocket_event_data_t`).

- [ ] **Step 2: Add a handshake-status getter**

In `ws_client.h`:

```c
// Returns the HTTP status from a rejected WS handshake since the last
// CONNECTED (e.g. 403 when the device_token was revoked), or 0 if none.
int ws_client_last_handshake_status(void);
```

In `ws_client.c`, add a static `s_last_handshake_status`, set it to 0 on `WEBSOCKET_EVENT_CONNECTED`, and on `WEBSOCKET_EVENT_ERROR` set it from the event's HTTP status when present:

```c
static int s_last_handshake_status = 0;

int ws_client_last_handshake_status(void) { return s_last_handshake_status; }

// in on_event:
case WEBSOCKET_EVENT_CONNECTED:
    s_last_handshake_status = 0;
    // ... existing ...
    break;
case WEBSOCKET_EVENT_ERROR:
    if (data->error_handle.esp_ws_handshake_status_code > 0)
        s_last_handshake_status = data->error_handle.esp_ws_handshake_status_code;
    // ... existing ...
    break;
```

> Field name (`esp_ws_handshake_status_code`) per the ESP-IDF `esp_websocket_client` version in `idf_component.yml`; confirm against the header while implementing.

- [ ] **Step 3: Build the firmware**

Run: `cd esp32-assistant && idf.py build`
Expected: compiles cleanly.

- [ ] **Step 4: Commit**

```bash
git add components/ws_client/ws_client.c components/ws_client/include/ws_client.h
git commit -m "feat(esp32): expose WS handshake status for revoke detection"
```

---

### Task E5: Wire resolve-or-pair + revoke handling into `main.c`

**Files:**
- Modify: `esp32-assistant/main/main.c`
- Modify: `esp32-assistant/main/Kconfig.projbuild`
- Modify: `esp32-assistant/main/CMakeLists.txt` (add `pairing` to `REQUIRES`/`PRIV_REQUIRES`)

**Interfaces:**
- Consumes: `aa_format_serial`, `aa_load_device_token`, `aa_run_pairing`, `aa_clear_device_token`, `aa_classify_disconnect`, `ws_client_last_handshake_status()`, the existing GOODBYE reason parse.

- [ ] **Step 1: Resolve the token before connecting**

After NVS init and WiFi connect (near line 787–844), add a resolver that produces the token passed to `ws_client_start` in place of `CONFIG_AA_DEVICE_TOKEN`:

```c
static char s_device_token[128];

static void show_pair_code(const char *code) {
    char line[16];
    snprintf(line, sizeof line, "%s", code);
    display_show("Pair code", line);   // headless units: code already ESP_LOGI'd in pairing_net
}

static const char *resolve_device_token(void) {
    // 1) explicit override (dev/legacy)
    if (CONFIG_AA_DEVICE_TOKEN[0] != '\0') {
        snprintf(s_device_token, sizeof s_device_token, "%s", CONFIG_AA_DEVICE_TOKEN);
        return s_device_token;
    }
    // 2) stored per-device token
    if (aa_load_device_token(s_device_token, sizeof s_device_token) > 0)
        return s_device_token;
    // 3) pair now
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char serial[13];
    aa_format_serial(mac, serial);
    char base[96];
    snprintf(base, sizeof base, "%s://%s:%d",
             CONFIG_AA_SERVER_SECURE ? "https" : "http",
             cfg.server_host, cfg.server_port);
    aa_run_pairing(base, serial, show_pair_code, s_device_token, sizeof s_device_token);
    return s_device_token;
}
```

Change the `ws_client_start(...)` call to pass `resolve_device_token()` instead of `CONFIG_AA_DEVICE_TOKEN`.

- [ ] **Step 2: Handle revoke after a WS session ends**

Wherever the connection lifecycle ends and would otherwise reconnect, classify first. Record the goodbye reason in the existing GOODBYE handler into a static `s_last_goodbye_reason`, then:

```c
int status = ws_client_last_handshake_status();
if (aa_classify_disconnect(status, s_last_goodbye_reason) == AA_DISCONNECT_REPAIR) {
    ESP_LOGW(TAG, "device token revoked -- clearing and re-pairing");
    display_show("Unpaired", "re-pairing");
    aa_clear_device_token();
    resolve_device_token();          // blocks in pairing, shows fresh code
    // then restart the ws client with the new s_device_token
}
s_last_goodbye_reason[0] = '\0';     // reset for next session
```

> Match this to the existing reconnect structure in `main.c` (the app has its own connect/idle state machine — insert the classify/repair branch at the point where it currently decides to reconnect). Keep `s_last_goodbye_reason` reset on each new CONNECTED.

- [ ] **Step 3: Update Kconfig help text**

In `Kconfig.projbuild`, change the `AA_DEVICE_TOKEN` help to state it is now an **optional override**: "If set, used verbatim and pairing is skipped (dev/legacy). Leave empty for normal per-device pairing."

- [ ] **Step 4: Build**

Run: `cd esp32-assistant && idf.py build`
Expected: compiles cleanly.

- [ ] **Step 5: On-device verification (manual)**

Flash a device with `CONFIG_AA_DEVICE_TOKEN` empty. Confirm, against a running gateway:
1. First boot shows a 6-digit code on the display and logs `pairing code: NNNNNN`.
2. Claiming the code in the web `Devices` screen transitions the device to connected; token persists in NVS across reboot (no code shown on second boot).
3. Pulling the network cable/AP → device shows reconnecting, keeps its token, reconnects when restored (network-drop path).
4. `Remove` in the web screen → device receives revoke, clears NVS token, shows "Unpaired / re-pairing", and displays a fresh code (revoke path).

- [ ] **Step 6: Commit**

```bash
git add main/main.c main/Kconfig.projbuild main/CMakeLists.txt
git commit -m "feat(esp32): pair on boot + wipe token on revoke, keep on drop"
```

---

## Self-Review notes

- **Spec coverage:** boot state machine (R6/E5), serial source (R1/E1), token store (R1/E2), show-code + headless log (R6/E1+E3+E5), override priority (R4/E5), revoke-vs-drop classifier (R3/E1, wired R6/E5), TTL re-init (R2/E3), auto re-pair on revoke (R6/E5), `clear_device_token` future hook (R1/E2). All covered.
- **Revoke-vs-drop emphasis (per request):** isolated as a pure, exhaustively-tested function on both platforms (R3, E1) and funnels every disconnect through one decision point (R6, E5). Token is wiped **only** on HTTP 401/403 handshake reject or `goodbye reason=account_disabled`; all other disconnects keep the token and reconnect.
- **No backend/web changes:** confirmed — only `rpi-assistant/` and `esp32-assistant/`.
