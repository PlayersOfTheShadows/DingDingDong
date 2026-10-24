"""Hosting-panel entry point.

Some panels (e.g. bot-hosting.net) run one file with the chosen runtime:
`python main.py`. The real entry is the `bot` package, which needs module
semantics (`python -m bot.main` on a local machine) for its relative imports.
This shim supports both: point the panel at `main.py`, or use
`python -m bot.main` from a console — same result.
"""
from __future__ import annotations

import asyncio

from bot.main import main

if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
