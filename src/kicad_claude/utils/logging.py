"""Logging configured to stderr.

CRITICAL: MCP servers using STDIO transport communicate over stdout. Any
print() or log handler attached to stdout corrupts the JSON-RPC stream and
breaks the connection. Always log to stderr.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """Configure root logging to stderr. Idempotent.

    Level resolution: explicit arg > KICAD_CLAUDE_LOG_LEVEL env > INFO.
    """
    global _CONFIGURED

    if level is None:
        level = os.environ.get("KICAD_CLAUDE_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if not _CONFIGURED:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(level)

    return logging.getLogger("kicad-claude")
