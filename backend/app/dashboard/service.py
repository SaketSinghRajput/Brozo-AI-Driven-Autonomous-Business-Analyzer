"""DashboardService (TASK-DASH-005, TASK-INTEGRATION-005; design §28).

build: executed plan steps + their evidence → WidgetSelector → Prioritizer → LayoutEngine, plus
filters (FilterBinder), insights and the executive summary. Every widget, insight and summary
sentence is validated through the ValidationService (persisted ``validation_results``), and its
status is what the spec carries: KPI cards as a number segment on ``result.value``, charts as
their cited evidence (incl. overlays), text as text segments whose numbers must match evidence.
The spec is a ``DashboardSpec`` (the model ``shared/schemas/dashboard_spec.schema.json`` is
exported from), so constructing it is the schema check. It is stored on the analysis run.

get: the latest dashboard of the version's current semantic model version (404 until built).
"""

import hashlib
from collections.abc import Sequence

from app.core.clock import Clock
from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.dashboard.domain import (
    ChartWidget,
    DashboardSpec,
    EvidencedText,
    Insight,
    KpiCardWidget,
    SummarySentence,
    Widget,
)
from app.dashboard.filters import FilterBinder
from app.dashboard.insights import explanations, growth_sentence, kpi_sentence
from app.dashboard.layout import LayoutEngine
from app.dashboard.selector import MetricFilter, Prioritizer, StepOutcome, WidgetSelector
from app.data.repository import DatasetRepository
from app.data.understanding_store import UnderstandingStore
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.orchestrator.composer import display_format
from app.planner.domain import AnalysisPlan, CapabilityReport
from app.planner.facts import DataFacts
from app.planner.runs import AnalysisRunRepository, StepRecord
from app.validator.domain import (
    AnswerSegment,
    NumberSegment,
    SubjectType,
    TextSegment,
    ValidationRequest,
    ValidationStatus,
)
from app.validator.service import ValidationService


class _Validate:
    """Runs the validator for one dashboard's widgets and texts; returns them with the status."""

    def __init__(
        self, service: ValidationService, scope: WorkspaceScope, dashboard_id: str, smv: int
    ) -> None:
        self._service = service
        self._scope = scope
        self._dashboard_id = dashboard_id
        self._smv = smv
        self._n = 0

    async def _status(
        self,
        kind: SubjectType,
        subject: str,
        evidence_ids: list[str],
        segments: list[AnswerSegment] | None,
    ) -> ValidationStatus:
        req = ValidationRequest(
            subject_type=kind,
            subject_id=f"{self._dashboard_id}:{subject}",
            segments=segments,
            evidence_ids=evidence_ids,
            semantic_model_version=self._smv,
        )
        return (await self._service.validate(self._scope, req)).status

    async def widget(self, widget: Widget) -> Widget:
        if isinstance(widget, KpiCardWidget) and widget.undefined_reason:
            # no number to check: the card states that the value is undefined, from evidence
            ref = widget.value_ref
            status = await self._status("widget", widget.id, [ref.evidence_id], None)
        elif isinstance(widget, KpiCardWidget):
            ref = widget.value_ref
            unit = widget.format.unit if widget.format else None
            fmt = display_format(unit)
            number = NumberSegment(evidence_id=ref.evidence_id, field=ref.field, format=fmt)
            status = await self._status("widget", widget.id, [ref.evidence_id], [number])
        elif isinstance(widget, ChartWidget):
            cited = [widget.chart.evidence_id, *(o.evidence_id for o in widget.chart.overlays)]
            status = await self._status("widget", widget.id, cited, None)
        else:
            return widget  # unavailable cards state a gap, not a claim
        return widget.model_copy(update={"validation_status": status})

    async def sentence(self, text: str, evidence_id: str) -> SummarySentence:
        self._n += 1
        segments: list[AnswerSegment] = [TextSegment(text=text)]
        status = await self._status("insight", f"summary{self._n}", [evidence_id], segments)
        return SummarySentence(text=text, evidence_ids=[evidence_id], validation_status=status)

    async def insight(
        self, iid: str, observed: EvidencedText, possible: list[EvidencedText]
    ) -> Insight:
        texts = [observed, *possible]
        cited = list(dict.fromkeys(e for t in texts for e in t.evidence_ids))
        segments: list[AnswerSegment] = [TextSegment(text=t.text + " ") for t in texts]
        status = await self._status("insight", iid, cited, segments)
        return Insight(
            id=iid, observed=observed, possible_explanations=possible, validation_status=status
        )


class DashboardService:
    def __init__(
        self,
        uow: UnitOfWork,
        validation: ValidationService,
        clock: Clock,
        prioritizer: Prioritizer | None = None,
    ) -> None:
        self._uow = uow
        self._validation = validation
        self._clock = clock
        self._prioritizer = prioritizer or Prioritizer()
        self._selector = WidgetSelector()
        self._layout = LayoutEngine()
        self._filters = FilterBinder()

    def outcomes(
        self, scope: WorkspaceScope, plan: AnalysisPlan, records: dict[str, StepRecord]
    ) -> list[StepOutcome]:
        found = []
        with self._uow(scope) as s:
            repo = EvidenceRepository(s)
            for step in plan.steps:
                record = records[step.id]
                evidence = repo.get(scope, record.evidence_id) if record.evidence_id else None
                rows = repo.rows(scope, evidence.evidence_id)[1] if evidence else None
                found.append(StepOutcome(step, record, evidence, rows))
        return found

    async def build(
        self,
        scope: WorkspaceScope,
        run_id: str,
        plan: AnalysisPlan,
        report: CapabilityReport,
        records: dict[str, StepRecord],
        facts: DataFacts,
        generic_only: bool,
    ) -> DashboardSpec:
        outcomes = self.outcomes(scope, plan, records)
        keep = MetricFilter(facts.model, self._prioritizer, generic_only)
        widgets = self._selector.select(plan.plan_id, outcomes, report, keep)
        widgets = self._prioritizer.order(widgets, facts.model)
        dashboard_id = f"db_{hashlib.sha256(run_id.encode()).hexdigest()[:26]}"
        validate = _Validate(self._validation, scope, dashboard_id, plan.semantic_model_version)
        widgets = [await validate.widget(w) for w in widgets]
        insights, summary = await self._texts(outcomes, widgets, keep, validate)
        return DashboardSpec(
            dashboard_id=dashboard_id,
            dataset_version_id=plan.dataset_version_id,
            plan_id=plan.plan_id,
            generated_at=self._clock.now(),
            filters=self._filters.filters(facts),
            widgets=widgets,
            layout=self._layout.layout(widgets),
            insights=insights,
            summary=summary,
        )

    async def _texts(
        self,
        outcomes: Sequence[StepOutcome],
        widgets: Sequence[Widget],
        keep: MetricFilter,
        validate: _Validate,
    ) -> tuple[list[Insight], list[SummarySentence]]:
        by_widget = {f"w_{o.step.id}": o.evidence for o in outcomes if o.evidence}
        insights: list[Insight] = []
        summary: list[SummarySentence] = []
        kpi = next((w for w in widgets if isinstance(w, KpiCardWidget)), None)
        kpi_ev = by_widget.get(kpi.id) if kpi else None
        if kpi is not None and kpi_ev is not None and (text := kpi_sentence(kpi.title, kpi_ev)):
            summary.append(await validate.sentence(text, kpi_ev.evidence_id))
        for o in outcomes:
            metric = str(o.step.args.get("metric", ""))
            if o.step.type != "growth_rate" or o.evidence is None or not keep(metric):
                continue
            text = growth_sentence(metric, o.evidence)
            if text is None:
                continue
            observed = EvidencedText(text=text, evidence_ids=[o.evidence.evidence_id])
            possible = [
                EvidencedText(text=t, evidence_ids=[c.evidence.evidence_id])
                for c in outcomes
                if c.step.type == "explain_change"
                and c.evidence is not None
                and c.step.args.get("metric") == metric
                for t in explanations(c.rows)
            ]
            insights.append(await validate.insight(f"i_{o.step.id}", observed, possible))
            if len(summary) < 3:
                summary.append(await validate.sentence(text, o.evidence.evidence_id))
        return insights, summary

    def get(self, scope: WorkspaceScope, dataset_id: str, version_id: str) -> DashboardSpec:
        with self._uow(scope) as s:
            version = DatasetRepository(s).get_version(scope, version_id)
            if version is None or version.dataset_id != dataset_id:
                raise NotFoundError("dataset version not found")
            model = UnderstandingStore(s).semantic_model(scope, version_id)
            runs = AnalysisRunRepository(s)
            stored = runs.latest_dashboard(scope, version_id, model.version) if model else None
            stale = stored is None
            if stale:  # re-planning a newer semantic model: serve the last one built
                stored = runs.latest_dashboard(scope, version_id, None)
        if stored is None:
            raise NotFoundError("the dashboard of this version is not ready yet")
        return DashboardSpec.model_validate({**stored, "stale": stale})
