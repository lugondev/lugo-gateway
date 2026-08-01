"""The name a freshly paired device gets when its owner didn't pick one.

The device already calls itself something the user can read off its own screen
during setup: the SSID of its provisioning AP, built by the firmware as
``"Lugo-%02X%02X"`` of the last two bytes of its MAC
(esp32-assistant/components/provisioning/provisioning_ssid.c). The pairing
serial is that same MAC as 12 hex characters, so the gateway can reproduce the
name from what pair/init already sends -- no extra field on the wire, no
firmware change.

Known coupling: the firmware derives the SSID from the STA interface MAC and the
serial from the efuse base MAC. On stock ESP-IDF those are the same address,
which is why this is exact today. A custom MAC configuration could make them
diverge and this name would then not match the AP -- cosmetic only, since it is
a suggestion the user can change.
"""

_FALLBACK = "New device"


def default_device_name(serial: str) -> str:
    """"Lugo-48D0" for serial "2884855048d0"; `_FALLBACK` if it's too short."""
    if len(serial) < 4:
        return _FALLBACK
    return f"Lugo-{serial[-4:].upper()}"
