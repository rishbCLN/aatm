#!/usr/bin/env python
"""AATM entry point.

Thin wrapper that ensures ``src`` is importable when running from a checkout, then
delegates to the CLI. Equivalent to ``python -m aatm.cli``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aatm.cli.commands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
