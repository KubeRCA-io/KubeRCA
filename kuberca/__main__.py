"""Entry point for `python -m kuberca`.

Usage:
    python -m kuberca
    uv run python -m kuberca
"""

from __future__ import annotations

import asyncio

from kuberca.app import main

asyncio.run(main())
