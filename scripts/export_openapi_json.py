#!/usr/bin/env python3
"""Write FastAPI OpenAPI schema to openapi/openapi-from-app.json (run from repo root)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "openapi" / "openapi-from-app.json"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from api.main import app  # noqa: PLC0415

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
