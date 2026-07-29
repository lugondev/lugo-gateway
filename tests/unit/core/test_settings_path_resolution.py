"""Task 2 (B1): db/artifacts/model paths must resolve against the repo root,
not the process CWD. See docs/superpowers/specs/2026-07-29-structure-audit-findings.md
and .superpowers/sdd/2026-07-29-structure-refactor/task-2-brief.md.

A run with CWD=apps/api_gateway/ used to silently create/read a second, stale
data/app.db + artifacts/ there instead of the repo-root ones -- these tests
pin the CWD-independent resolver functions directly (not the global `settings`
singleton) so they can't be defeated by a leftover monkeypatch elsewhere.
"""

from pathlib import Path

from app.core.settings import APP_ROOT, Settings, resolve_sqlite_url, resolve_under_root


def test_resolve_under_root_anchors_relative_path_to_root_regardless_of_cwd(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    monkeypatch.chdir(root)
    at_root = resolve_under_root("artifacts", root=root)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    away_from_root = resolve_under_root("artifacts", root=root)

    assert at_root == away_from_root == str(root / "artifacts")


def test_resolve_under_root_leaves_absolute_paths_unchanged():
    # e.g. the model_service image's ARTIFACTS_DIR=/tmp/artifacts override.
    assert resolve_under_root("/tmp/artifacts", root=Path("/some/repo")) == "/tmp/artifacts"


def test_resolve_sqlite_url_anchors_relative_path_regardless_of_cwd(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    expected = f"sqlite+aiosqlite:///{root / 'data' / 'app.db'}"

    monkeypatch.chdir(root)
    at_root = resolve_sqlite_url("sqlite+aiosqlite:///data/app.db", root=root)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    away_from_root = resolve_sqlite_url("sqlite+aiosqlite:///data/app.db", root=root)

    assert at_root == away_from_root == expected


def test_resolve_sqlite_url_passes_through_postgres_unchanged():
    url = "postgresql+asyncpg://u:p@host/db"
    assert resolve_sqlite_url(url, root=Path("/some/repo")) == url


def test_resolve_sqlite_url_passes_through_absolute_sqlite_unchanged():
    # The model_service image's DATABASE_URL override -- 4 slashes, absolute.
    url = "sqlite+aiosqlite:////tmp/model_service.db"
    assert resolve_sqlite_url(url, root=Path("/some/repo")) == url


def test_resolve_sqlite_url_passes_through_in_memory_unchanged():
    url = "sqlite+aiosqlite:///:memory:"
    assert resolve_sqlite_url(url, root=Path("/some/repo")) == url


def test_default_database_url_resolves_to_repo_root_data_app_db():
    """The DEFAULT target must not move: from repo root, this is the exact
    same file the app has always used -- just no longer CWD-dependent."""
    s = Settings(_env_file=None)
    assert s.database_url == "sqlite+aiosqlite:///data/app.db"  # raw field untouched
    assert s.database_url_resolved == f"sqlite+aiosqlite:///{APP_ROOT / 'data' / 'app.db'}"


def test_default_artifacts_dir_resolves_to_repo_root_artifacts():
    s = Settings(_env_file=None)
    assert s.artifacts_dir == "artifacts"  # raw field untouched
    assert s.artifacts_dir_resolved == str(APP_ROOT / "artifacts")


def test_default_stt_model_dir_resolves_to_repo_root_models_stt():
    s = Settings(_env_file=None)
    assert s.stt_model_dir == "models/stt"  # raw field untouched
    assert s.stt_model_dir_resolved == str(APP_ROOT / "models" / "stt")


def test_absolute_database_url_override_wins_unchanged():
    s = Settings(_env_file=None, database_url="sqlite+aiosqlite:////tmp/model_service.db")
    assert s.database_url_resolved == "sqlite+aiosqlite:////tmp/model_service.db"


def test_absolute_artifacts_dir_override_wins_unchanged():
    s = Settings(_env_file=None, artifacts_dir="/tmp/artifacts")
    assert s.artifacts_dir_resolved == "/tmp/artifacts"


def test_app_root_contains_pyproject_toml():
    assert (APP_ROOT / "pyproject.toml").is_file()
