"""TASK-CHAT-001/002/003 + AGENT-007/010 end to end on PostgreSQL (app role, RLS on):
conversation API, SSE TurnEvent stream in the documented order, persisted answer + claims +
validation + evidence, owner-only access, and degraded mode with the LLM down.
The LLM is the scripted FakeLLM; everything else is the real stack."""

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql

from app.core.scope import WorkspaceScope
from app.evidence.repository import EvidenceRepository
from app.llm.errors import LLMFailure, LLMFailureReason
from app.tools.permissions import Role
from tests.integration.analytics.test_analytics_tools import _seed
from tests.integration.auth_helpers import add_user, container_of, csrf_headers, login, make_app
from tests.integration.conftest import TestDatabase
from tests.unit.orchestrator.fakes import (
    AUG,
    DONE,
    NORTH,
    Step,
    compose,
    gateway,
    intent,
    num,
    text,
)

URL = "/api/v1/chat/conversations"


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client


def _setup(api: TestClient, script: list[Step]) -> tuple[dict[str, str], str]:
    container = container_of(api)
    seeded = _seed(container.uow, container.storage)  # type: ignore[arg-type]
    gw, _ = gateway(script)
    api.app.state.container = dataclasses.replace(container, llm=gw)  # type: ignore[attr-defined]
    email = add_user(container, seeded["ws"], Role.ANALYST)
    return seeded, login(api, email)


def _stream(api: TestClient, csrf: str, cid: str, question: str) -> list[dict[str, Any]]:
    r = api.post(f"{URL}/{cid}/messages", json={"content": question}, headers=csrf_headers(csrf))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*r.text.splitlines(), ""]:
        if line.startswith("event:"):
            current["event"] = line.split(":", 1)[1].strip()
        elif line.startswith("id:"):
            current["id"] = int(line.split(":", 1)[1])
        elif line.startswith("data:"):
            envelope = json.loads(line.split(":", 1)[1])
            assert envelope["schema_version"] == "1.0"
            assert envelope["seq"] == current["id"]
            current["data"] = envelope["event"]
        elif not line.strip() and current:
            events.append(current)
            current = {}
    return events


def _conversation(api: TestClient, csrf: str, seeded: dict[str, str]) -> str:
    body = {"dataset_version_id": seeded["dsv"], "semantic_model_version": 1}
    r = api.post(URL, json=body, headers=csrf_headers(csrf))
    assert r.status_code == 201, r.text
    cid: str = r.json()["id"]
    return cid


def test_metric_turn_streams_documented_events_and_persists_the_answer(api: TestClient) -> None:
    seeded, csrf = _setup(
        api,
        [
            intent(metrics=["Revenue"], filters=NORTH, **AUG),
            DONE,
            compose(text("Revenue in North in August 2026 was "), num("n1"), text(".")),
        ],
    )
    cid = _conversation(api, csrf, seeded)
    events = _stream(api, csrf, cid, "What was revenue in North in August 2026?")
    assert [e["id"] for e in events] == list(range(len(events)))
    kinds = [e["event"] if e["event"] != "status" else e["data"]["stage"] for e in events]
    assert kinds == [
        "CLASSIFYING",
        "PLANNING",
        "EXECUTING",
        "tool_call",
        "COMPOSING",
        "VALIDATING",
        "validation",
        "segment",
        "segment",
        "segment",
        "final",
    ]
    assert all(e["data"]["type"] == e["event"] for e in events)
    final = events[-1]["data"]
    assert (final["state"], final["validation_status"]) == ("COMPLETED", "VALID")
    number = events[8]["data"]["segment"]
    assert (number["value"], number["unit"]) == (200.0, "currency:INR")
    tool_evidence = events[3]["data"]["evidence_id"]
    assert number["evidence_id"] == tool_evidence

    container = container_of(api)
    scope = WorkspaceScope(workspace_id=seeded["ws"], principal_id="usr_check")
    with container.uow(scope) as s:
        stored = EvidenceRepository(s).get(scope, tool_evidence)
        claim = s.execute(
            sql("SELECT text_span, validation_id FROM claims WHERE :ev = ANY(evidence_ids)"),
            {"ev": tool_evidence},
        ).one()
        turn = s.execute(
            sql("SELECT turn, validation_id FROM messages WHERE id = :id"),
            {"id": final["message_id"]},
        ).one()
    assert stored is not None
    assert stored.result.value == 200.0  # the shown number is the evidence value
    assert claim.text_span == "Revenue in North in August 2026 was INR 200.00."
    assert claim.validation_id == turn.validation_id == events[6]["data"]["validation_id"]
    assert turn.turn["states"][-1] == "COMPLETED"

    messages = api.get(f"{URL}/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["state"] == "COMPLETED"
    assert messages[1]["segments"][1] == {  # reopened history renders the evidence value
        "type": "number",
        "evidence_id": tool_evidence,
        "field": "result.value",
        "format": "currency:INR:2",
        "value": 200.0,
        "unit": "currency:INR",
        "absolute": False,  # no direction word governs it (B-5)
    }
    assert messages[1]["validation_status"] == "VALID"
    assert messages[1]["confidence"] == events[6]["data"]["confidence"]
    assert messages[0]["validation_status"] is None  # the question
    assert messages[1]["answer_source"] is None  # the assistant's own wording


def test_wording_only_failure_is_answered_from_the_template_and_stored_so(
    api: TestClient,
) -> None:
    causal = compose(text("Revenue fell "), num("n1"), text(" due to weaker Apparel sales."))
    seeded, csrf = _setup(
        api, [intent(metrics=["Revenue"], filters=NORTH, **AUG), DONE, causal, causal]
    )
    cid = _conversation(api, csrf, seeded)
    events = _stream(api, csrf, cid, "Why did revenue drop in North in Aug 2026?")
    final = events[-1]["data"]
    assert (final["state"], final["validation_status"]) == ("COMPLETED", "VALID")
    assert final["answer_source"] == "template"
    messages = api.get(f"{URL}/{cid}/messages").json()
    assert messages[1]["answer_source"] == "template"


def test_conversations_are_owner_only_except_for_admins(api: TestClient) -> None:
    seeded, csrf = _setup(api, [])
    cid = _conversation(api, csrf, seeded)
    assert [c["id"] for c in api.get(URL).json()] == [cid]
    container = container_of(api)
    login(api, add_user(container, seeded["ws"], Role.ANALYST))  # a colleague
    assert api.get(f"{URL}/{cid}").status_code == 404
    assert api.get(f"{URL}/{cid}/messages").status_code == 404
    assert api.get(URL).json() == []
    login(api, add_user(container, seeded["ws"], Role.ADMIN))
    assert api.get(f"{URL}/{cid}").json()["id"] == cid


def test_unknown_dataset_version_is_not_found(api: TestClient) -> None:
    _, csrf = _setup(api, [])
    body = {"dataset_version_id": "dsv_missing", "semantic_model_version": 1}
    assert api.post(URL, json=body, headers=csrf_headers(csrf)).status_code == 404


def test_llm_down_streams_degraded_mode_while_analytics_keep_working(api: TestClient) -> None:
    down = LLMFailure(LLMFailureReason.UNAVAILABLE, "connection refused")
    seeded, csrf = _setup(api, [down, down, down])
    cid = _conversation(api, csrf, seeded)
    events = _stream(api, csrf, cid, "What was revenue?")
    assert [e["event"] for e in events] == ["status", "error", "segment", "final"]
    assert events[1]["data"]["error"]["error_code"] == "LLM_ERROR"
    assert (events[-1]["data"]["mode"], events[-1]["data"]["state"]) == ("degraded", "RECOVERED")
    body = {
        "dataset_version_id": seeded["dsv"],
        "semantic_model_version": 1,
        "request": {"metric": "Revenue"},
    }
    r = api.post("/api/v1/analytics/query", json=body, headers=csrf_headers(csrf))
    assert r.status_code == 200
    assert r.json()["value"] == 885.0  # fixture: Jul 345 + Aug 540
