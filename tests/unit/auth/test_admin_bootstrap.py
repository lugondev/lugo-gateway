import pytest

from app.core.settings import settings
from app.main import _bootstrap_admin_if_needed
from app.services.auth.users import user_store


@pytest.mark.asyncio
async def test_bootstrap_creates_admin_from_bootstrap_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "root")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "r00t-pw")
    await _bootstrap_admin_if_needed()
    user = await user_store.get_by_username("root")
    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_bootstrap_falls_back_to_legacy_admin_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    monkeypatch.setattr(settings, "admin_password", "legacy-pw")
    await _bootstrap_admin_if_needed()
    user = await user_store.get_by_username("admin")
    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_bootstrap_noop_when_users_already_exist(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "root")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "r00t-pw")
    await user_store.create("someone", "already-here")
    await _bootstrap_admin_if_needed()
    assert await user_store.get_by_username("root") is None


@pytest.mark.asyncio
async def test_bootstrap_noop_when_no_credentials_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_bootstrap_username", "")
    monkeypatch.setattr(settings, "admin_bootstrap_password", "")
    monkeypatch.setattr(settings, "admin_password", "")
    await _bootstrap_admin_if_needed()
    assert await user_store.count() == 0
