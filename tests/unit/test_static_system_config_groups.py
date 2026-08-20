"""The admin System Config panel renders one collapsible per group, from a
hard-coded GROUPS list in system-config.js.

A group present in SystemConfig and in /v1/system/config/meta but missing from
that list renders nowhere: the fields exist, carry labels and help text, and no
operator can ever reach them. That is exactly what happened to `knowledge` --
Field(title=..., description=..., subgroup=...) was added and the plan assumed
"the fields appear without further work".
"""

from pathlib import Path

from pydantic import BaseModel

from app.services.system_config import SystemConfig

JS = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app" / "static" / "js"


def test_the_static_editor_renders_every_system_config_group():
    text = (JS / "system-config.js").read_text(encoding="utf-8")
    missing = [
        name
        for name, info in SystemConfig.model_fields.items()
        if isinstance(info.annotation, type)
        and issubclass(info.annotation, BaseModel)
        and f'key: "{name}"' not in text
    ]
    assert not missing, (
        "SystemConfig group(s) with no entry in system-config.js's GROUPS -- their "
        "fields render in no UI, whatever metadata the meta route exposes: "
        + ", ".join(missing)
    )
