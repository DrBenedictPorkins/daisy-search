"""FastAPI application factory."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from daisy import __version__

if TYPE_CHECKING:
    pass

# Paths
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

# Jinja2 templates — module-level so routes can import it
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# --- Jinja2 custom filters ---


def _format_date(value: datetime | date | None) -> str:
    """Format a datetime or date as a human-readable string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.strftime("%Y-%m-%d")


def _confidence_class(value: float) -> str:
    """Return a CSS class for a confidence bar fill color."""
    if value >= 0.7:
        return "confidence-high"
    if value >= 0.4:
        return "confidence-medium"
    return "confidence-low"


def _truncate_url(url: str, max_length: int = 60) -> str:
    """Truncate a URL for display, keeping the domain visible."""
    if len(url) <= max_length:
        return url
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path
    if len(domain) + len(path) <= max_length:
        return f"{domain}{path}"
    remaining = max_length - len(domain) - 3  # 3 for "..."
    if remaining > 0:
        return f"{domain}{path[:remaining]}..."
    return url[:max_length] + "..."


def _filesizeformat(value: int) -> str:
    """Format a byte count as a human-readable file size."""
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


# Register custom filters
templates.env.filters["format_date"] = _format_date
templates.env.filters["confidence_class"] = _confidence_class
templates.env.filters["truncate_url"] = _truncate_url
templates.env.filters["filesizeformat"] = _filesizeformat

# Add version as a global template variable
templates.env.globals["version"] = __version__


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title="Daisy Search",
        version=__version__,
        description="Personal identity exposure and OSINT research system",
    )

    # Static files
    application.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    @application.get("/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    # Dashboard routes (read-only web UI)
    from daisy.api.routes.dashboard import router as dashboard_router

    application.include_router(dashboard_router)

    # Generic HTML extraction endpoint (trafilatura — always available)
    from daisy.api.routes.extract import router as extract_router

    application.include_router(extract_router)

    return application


app = create_app()
