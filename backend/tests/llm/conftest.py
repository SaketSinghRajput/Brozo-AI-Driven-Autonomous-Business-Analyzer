"""Live local-LLM tests: skipped unless APP_LLM_TESTS=1 (normal `pytest` never needs a model).

Run: `APP_LLM_TESTS=1 uv run pytest -m llm tests/llm` with Ollama serving APP_LLM__MODEL
(default qwen2.5:7b) on 127.0.0.1:11434.
"""

import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("APP_LLM_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live LLM test; set APP_LLM_TESTS=1 to run")
    for item in items:
        # get_closest_marker, not `in item.keywords`: keywords also contain directory names,
        # which would silently skip everything under tests/unit/llm/.
        if item.get_closest_marker("llm") is not None:
            item.add_marker(skip)
