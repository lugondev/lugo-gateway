import uuid
from datetime import datetime, timezone

import pytest

from app.services.db.engine import db_session, init_db
from app.services.db.models import UsageEvent
from app.services.usage.query import summarize, summarize_for_user


def _row(*, user_id, provider_id, kind, engine, model_id, cost_usd, native_amount, ts=None):
    return UsageEvent(
        id=str(uuid.uuid4()),
        ts=ts or datetime.now(timezone.utc),
        user_id=user_id,
        profile_id="",
        provider_id=provider_id,
        kind=kind,
        engine=engine,
        model_id=model_id,
        unit="tokens",
        native_amount=native_amount,
        cost_usd=cost_usd,
    )


async def _seed():
    await init_db()
    async with db_session() as s:
        s.add_all([
            _row(user_id="u1", provider_id="prov-a", kind="llm", engine="openrouter",
                 model_id="qwen-max", cost_usd=1.5, native_amount=1000,
                 ts=datetime(2026, 1, 15, tzinfo=timezone.utc)),
            _row(user_id="u1", provider_id="prov-a", kind="llm", engine="openrouter",
                 model_id="qwen-max", cost_usd=2.5, native_amount=2000,
                 ts=datetime(2026, 1, 20, tzinfo=timezone.utc)),
            _row(user_id="u2", provider_id="prov-b", kind="tts", engine="vieneu",
                 model_id="v1", cost_usd=0.25, native_amount=500,
                 ts=datetime(2026, 2, 5, tzinfo=timezone.utc)),
        ])
        await s.commit()


async def test_summarize_by_provider_sums_and_counts():
    await _seed()
    result = await summarize("provider")
    by_key = {r["key"]: r for r in result}
    assert by_key["prov-a"]["count"] == 2
    assert abs(by_key["prov-a"]["cost_usd"] - 4.0) < 1e-9
    assert abs(by_key["prov-a"]["native_amount"] - 3000) < 1e-9
    assert by_key["prov-b"]["count"] == 1
    assert abs(by_key["prov-b"]["cost_usd"] - 0.25) < 1e-9


async def test_summarize_by_kind():
    await _seed()
    result = await summarize("kind")
    by_key = {r["key"]: r for r in result}
    assert by_key["llm"]["count"] == 2
    assert abs(by_key["llm"]["cost_usd"] - 4.0) < 1e-9
    assert by_key["tts"]["count"] == 1


async def test_summarize_period_filter_restricts_to_month():
    await _seed()
    result = await summarize("kind", period_key="2026-01")
    by_key = {r["key"]: r for r in result}
    assert "llm" in by_key
    assert "tts" not in by_key
    assert by_key["llm"]["count"] == 2


async def test_summarize_unknown_group_by_raises():
    await _seed()
    with pytest.raises(ValueError):
        await summarize("bogus")


async def test_summarize_for_user_scopes_and_groups_by_kind_model():
    await _seed()
    result = await summarize_for_user("u1")
    assert len(result) == 1
    row = result[0]
    assert row["kind"] == "llm" and row["model_id"] == "qwen-max"
    assert row["count"] == 2
    assert abs(row["cost_usd"] - 4.0) < 1e-9

    other = await summarize_for_user("u2")
    assert len(other) == 1
    assert other[0]["kind"] == "tts"


async def test_summarize_for_user_groups_by_engine_too():
    await init_db()
    from app.services.usage.recorder import record_usage

    await record_usage(user_id="u-eng", profile_id="p", kind="stt", engine="engine-a",
                       model_id="m1", unit="seconds", native_amount=10)
    await record_usage(user_id="u-eng", profile_id="p", kind="stt", engine="engine-b",
                       model_id="m1", unit="seconds", native_amount=5)

    rows = await summarize_for_user("u-eng")
    # Same kind + model, different engines -> two rows, each naming its engine.
    assert {(r["kind"], r["engine"], r["model_id"]) for r in rows} == {
        ("stt", "engine-a", "m1"),
        ("stt", "engine-b", "m1"),
    }
    assert {r["native_amount"] for r in rows} == {10.0, 5.0}
