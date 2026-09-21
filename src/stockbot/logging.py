"""Shared logging setup for the bot and market processes.

Both mains used to `basicConfig` their own slightly different formats;
one setup keeps field names consistent so `docker logs` parses the same
way for every service. Plain single-line format (key=value in messages
by convention), not JSON -- log lines here are for humans tailing
docker-compose output first, aggregators second.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(service: str, level: int = logging.INFO) -> None:
    """Root logger with the standard stockbot line format. Call once at
    process start; idempotent (re-running resets handlers, which is what
    you want if a library already configured something)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(f"%(asctime)s {service} %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
