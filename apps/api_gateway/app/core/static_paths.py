"""Filesystem locations of the admin web UI, anchored to APP_ROOT.

These were the last two CWD-relative paths in the app: main.py mounted
``StaticFiles(directory="apps/api_gateway/app/static")`` and routes/ui.py served
``FileResponse("apps/api_gateway/app/static/index.html")``. StaticFiles checks
its directory at CONSTRUCTION, so a process launched from anywhere but the repo
root did not merely 404 the UI -- it failed to import at all:

    RuntimeError: Directory 'apps/api_gateway/app/static' does not exist

Same bug class settings.py already closed for the DB, artifacts and model dirs
(APP_ROOT / resolve_under_root, the documented B1 fix); these two were simply
missed because nothing reads them through Settings.
"""

from pathlib import Path

from app.core.settings import APP_ROOT

STATIC_DIR: str = str(APP_ROOT / "apps" / "api_gateway" / "app" / "static")
INDEX_HTML: str = str(Path(STATIC_DIR) / "index.html")
