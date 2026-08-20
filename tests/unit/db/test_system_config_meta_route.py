from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import app
from app.services.system_config import SystemConfig


def _config_groups() -> set[str]:
    """Every nested model on SystemConfig -- i.e. every group with fields to render."""
    return {
        name
        for name, info in SystemConfig.model_fields.items()
        if isinstance(info.annotation, type) and issubclass(info.annotation, BaseModel)
    }


def test_meta_endpoint_covers_every_group_on_system_config():
    """Derived, never a literal.

    This assertion used to hard-code {engines, conversation, preprocessing}. A
    whole new group (`knowledge`) was added to SystemConfig with full
    title/description/subgroup metadata, rendered in no UI at all, and this
    test stayed green -- because the literal it compared against was itself the
    thing that needed updating. Deriving from model_fields means the next group
    added fails here until _field_meta is told about it.
    """
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert set(data.keys()) == _config_groups()


def test_meta_endpoint_has_no_stale_field_names():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert "conversation_stt_engine" not in data["conversation"]
    assert "conversation_tts_engine" not in data["conversation"]
    assert "pyannote_vad_model" not in data["preprocessing"]


def test_meta_entry_shape_for_a_representative_field():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    entry = data["engines"]["default_stt_engine"]
    assert entry["label"] == "Default STT engine"
    assert "standalone transcription" in entry["description"]
    assert entry["subgroup"] == "Engine selection"
    assert entry["unit"] is None
    assert entry["multiline"] is False


def test_meta_marks_the_system_prompt_field_multiline():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    assert data["conversation"]["conversation_system_prompt"]["multiline"] is True


def test_meta_groups_conversation_fields_into_four_subgroups():
    client = TestClient(app)
    data = client.get("/v1/system/config/meta").json()["data"]
    subgroups = {entry["subgroup"] for entry in data["conversation"].values()}
    assert subgroups == {"Timing & VAD", "STT", "TTS & Audio", "Language & Prompt"}
