# TASK-SEC-001 fixture: intentionally violates CLAUDE.md section 20 (Local AI Policy).
# Used only by test_check_forbidden_providers.py to prove a real violation is detected.
# Allow-listed in scripts/forbidden_providers_allowlist.txt so it doesn't fail the repo-wide scan.
import openai  # noqa: F401
