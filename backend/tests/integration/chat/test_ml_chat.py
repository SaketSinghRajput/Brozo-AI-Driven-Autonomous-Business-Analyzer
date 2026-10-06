"""GAPS-2 on PostgreSQL: a forecast question through ChatService + the real orchestrator stack.

Upload -> pipeline -> READY, then a chat turn (scripted FakeLLM) whose plan comes from the
capability report and templates, whose ML numbers cite the evidence's stored result rows, and
whose ValidationService loads those rows from the database (RLS on, app role).
"""

import asyncio
import dataclasses

from app.chat.service import ChatService, build_orchestrator
from app.core.scope import WorkspaceScope
from app.orchestrator.domain import AnyTurnEvent, FinalEvent, NumberView, SegmentEvent, TurnState
from app.security.bootstrap import BOOTSTRAP
from app.security.repository import AuthRepository
from app.tools.permissions import Role
from app.validator.domain import ValidationStatus
from tests.principals import principal_for
from tests.unit.orchestrator.fakes import DONE, compose, gateway, intent, text

from ..dataset_env import FIXTURES, Env


def test_forecast_turn_validates_against_stored_rows(env: Env) -> None:
    r = env.upload(FIXTURES / "retail" / "orders.csv")
    assert r.status_code == 201, r.text
    ids = r.json()
    env.settle()
    model = env.get(f"/{ids['dataset_id']}/versions/{ids['version_id']}/semantic-model").json()

    user_id = env.container.ids.new("usr")
    with env.container.uow(BOOTSTRAP) as s:
        repo = AuthRepository(s)
        repo.create_user(BOOTSTRAP, user_id, f"{user_id}@example.test", "x")
        repo.add_membership(BOOTSTRAP, user_id, env.scope.workspace_id, Role.ANALYST)
    scope = WorkspaceScope(workspace_id=env.scope.workspace_id, principal_id=user_id)

    script = [intent(objective="forecast", metrics=["Revenue"]), DONE, compose(text("Outlook:"))]
    container = dataclasses.replace(env.container, llm=gateway(script)[0])
    chat = ChatService(container.uow, container.ids, container.clock)
    conversation = chat.create(scope, ids["version_id"], model["version"], "forecast")
    req = chat.start_turn(scope, principal_for(scope), conversation, "Forecast revenue", "t-1")
    assert req.facts is not None

    async def collect() -> list[AnyTurnEvent]:
        return [e async for e in build_orchestrator(container).handle_turn(req)]

    events = asyncio.run(collect())
    final = events[-1]
    assert isinstance(final, FinalEvent)
    assert (final.state, final.validation_status) == (TurnState.COMPLETED, ValidationStatus.VALID)
    numbers = [
        e.segment
        for e in events
        if isinstance(e, SegmentEvent) and isinstance(e.segment, NumberView)
    ]
    assert numbers
    assert numbers[0].field == "rows.0.value"
    assert all(n.value is not None for n in numbers)
