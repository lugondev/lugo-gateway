from pydantic import BaseModel


class PairInitRequest(BaseModel):
    serial: str


class PairClaimRequest(BaseModel):
    code: str
    # Optional: blank means "name it after the device". The gateway fills in the
    # SSID the device shows on its own setup AP (services/auth/device_naming.py),
    # so pairing can finish on the code alone. Clients that send a name still win.
    name: str = ""
    # Bind the device to an assistant in the same call that creates it. The web
    # client pairs from inside a profile, so it always knows the answer here, and
    # doing it in one step removes the window where a device exists but answers
    # to nothing. "" (the default) pairs the device unassigned, which is what the
    # older clients that don't send this field get.
    profile_id: str = ""


class DeviceProfileRequest(BaseModel):
    """Body of POST /v1/devices/mine/{device_id}/profile. "" unassigns."""

    profile_id: str = ""


class DeviceNameRequest(BaseModel):
    """Body of POST /v1/devices/mine/{device_id}/name."""

    name: str
