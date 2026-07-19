"""Shared Jinja2 template environment.

Defined in its own module so both ``app.main`` and the routers under
``app.routers`` render from a single ``Jinja2Templates`` instance without
importing ``app.main`` (which would create a circular import: main includes the
routers, the routers would import main).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

# Package root (the directory containing this module), so the templates
# directory resolves the same way regardless of the process working directory.
BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
