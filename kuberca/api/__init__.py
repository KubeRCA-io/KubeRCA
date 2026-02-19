"""REST API layer for KubeRCA.

Exposes:
    create_app -- FastAPI application factory.
    build_app  -- Alias for create_app (used by kuberca.app bootstrap).
"""

from kuberca.api.app import create_app

# The bootstrap in kuberca.app imports `build_app` from this package.
build_app = create_app

__all__ = ["build_app", "create_app"]
