#!/usr/bin/env python3
"""Entry point: ``python transcribe_stream.py -o out.txt`` (see radio_hls.transcribe)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from radio_hls.transcribe import cli

if __name__ == "__main__":
    cli()
