"""The app must import from any working directory.

settings.py deliberately anchors every path-shaped setting to APP_ROOT rather
than the process CWD (`_default_app_root`, `resolve_under_root`,
`resolve_sqlite_url` -- the documented B1 fix). Two CWD-relative paths were
never converted: main.py's StaticFiles mount and routes/ui.py's FileResponse.
StaticFiles validates its directory at construction, so a process started
anywhere but the repo root died at import with

    RuntimeError: Directory 'apps/api_gateway/app/static' does not exist

A subprocess is the only honest way to assert this: by the time a test runs,
app.main is already imported from the repo root.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

_GATEWAY = Path(__file__).resolve().parents[3] / "apps" / "api_gateway"


def _import_app_from(cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "import app.main; print('ok')"],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_GATEWAY), "HOME": str(Path.home())},
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_app_imports_from_an_unrelated_working_directory():
    with tempfile.TemporaryDirectory() as elsewhere:
        result = _import_app_from(elsewhere)
    assert result.returncode == 0, result.stderr[-3000:]


def test_static_mount_and_ui_page_resolve_under_app_root():
    from app.core.settings import APP_ROOT
    from app.core.static_paths import INDEX_HTML, STATIC_DIR

    assert Path(STATIC_DIR).is_absolute()
    assert Path(STATIC_DIR).is_relative_to(APP_ROOT)
    assert Path(INDEX_HTML).is_file()
