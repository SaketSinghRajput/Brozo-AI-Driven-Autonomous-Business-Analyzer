#!/usr/bin/env python3
"""Export the backend's OpenAPI schema to shared/openapi.json (TASK-INFRA-003).

Used by CI to detect drift between the FastAPI app and the committed contract that the
frontend generates its client from: run this, then `git diff --exit-code shared/openapi.json`.

Builds the app with the same no-database test config as backend/tests/conftest.py (see
`AppConfig` in backend/app/core/config.py) so this never needs a running Postgres.
"""

from __future__ import annotations

import difflib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
OUTPUT = REPO_ROOT / "shared" / "openapi.json"


def render() -> str:
    """The OpenAPI document exactly as it is written to shared/openapi.json."""
    sys.path.insert(0, str(BACKEND))

    from app.core.config import AppConfig, Environment
    from app.main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            _env_file=None,
            env=Environment.TEST,
            database={
                "url": "postgresql+psycopg://test:test@127.0.0.1:5432/test",
                "check_schema_on_startup": False,
            },
            storage={"datastore_path": Path(tmp) / "datastore"},
            security={"session_secret": "t" * 32},
        )
        app = create_app(config)
        schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    check = "--check" in sys.argv[1:]
    text = render()
    if check:  # TASK-TEST-005: fail on drift without touching the file
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            diff = difflib.unified_diff(
                current.splitlines(), text.splitlines(), "committed", "generated", lineterm=""
            )
            print("\n".join(list(diff)[:80]))
            print(f"{OUTPUT.relative_to(REPO_ROOT)} is out of date: run scripts/export_openapi.py")
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
