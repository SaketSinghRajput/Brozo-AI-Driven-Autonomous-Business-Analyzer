"""``ml.segments`` tool (TASK-ML-022, TRD 17.1): segments only when they are real.

Entities → features → K-Means/HDBSCAN candidates → quality & stability gates → profiles with
descriptions built from the profile numbers. Refused with ``LOW_MODEL_QUALITY`` (and the
numbers) when no candidate passes the gates — no invented segments.
"""

from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, JsonValue

from app.core.scope import WorkspaceScope
from app.ml.anomaly import SKLEARN
from app.ml.detector import DataCharacteristics, MLTaskDetector
from app.ml.domain import (
    Metrics,
    MLTask,
    ModelInfo,
    ModelInputs,
    ModelResult,
    Selection,
    TaskSpec,
    TrainingInfo,
)
from app.ml.segments import SegmentProfile, candidates, choose, entity_features, profile, scale
from app.ml.tooling import MLTool, calculation
from app.ml.trainer import Trainer
from app.planner.domain import ReasonCode, Unavailability
from app.tools.domain import ToolMetadata, ToolPermission


class SegmentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_column: str  # e.g. customer_id
    measures: list[str] = []  # numeric columns; default = every numeric column
    time_column: str | None = None  # adds recency_days / tenure_days
    method: Literal["auto", "kmeans", "hdbscan"] = "auto"
    seed: int | None = None


class SegmentsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_run_id: str
    rows: int  # entities clustered
    calculation: str  # recorded on the evidence: model, library version, run id
    segments: list[SegmentProfile]
    model_result: ModelResult
    selection: Selection


class SegmentsTool(MLTool):
    meta = ToolMetadata(
        name="ml.segments",
        version="1.0.0",
        description="Segment the entities (e.g. customers) of the current dataset version by "
        "their activity with K-Means or HDBSCAN. Returns segments with sizes, feature means and "
        "descriptions, plus silhouette and stability. Refuses when the segments are not "
        "well separated or not stable.",
        permission=ToolPermission.MODEL_RUN,
        side_effects="none",
        timeout_s=180,
        max_calls_per_turn=2,
        parallel_safe=True,
        runs_in="worker",
    )
    input_model: type[BaseModel] = SegmentsArgs
    output_model: type[BaseModel] = SegmentsOutput

    def run(
        self, args: SegmentsArgs, frame: pl.DataFrame, scope: WorkspaceScope, version_id: str
    ) -> SegmentsOutput | Unavailability:
        seed = self.seed(args.seed)
        measures = args.measures or [
            c for c in frame.columns if c != args.entity_column and frame[c].dtype.is_numeric()
        ]
        feats = entity_features(frame, args.entity_column, measures, args.time_column)
        columns = [c for c in feats.columns if c != args.entity_column]
        spec = TaskSpec(task=MLTask.SEGMENTATION, dataset_version_id=version_id, seed=seed)
        chars = DataCharacteristics(
            rows=frame.height, entities=feats.height, numeric_features=len(measures)
        )
        refused = MLTaskDetector(self._cfg).detect(None, spec, chars)
        if isinstance(refused, Unavailability):
            return refused
        x = scale(feats, columns)
        methods: set[str] = {"kmeans", "hdbscan"} if args.method == "auto" else {args.method}
        fitted = Trainer(self._cfg).fit(
            "hdbscan" if "hdbscan" in methods else "kmeans",
            lambda m: candidates(m, seed, self._cfg, methods),
            x,
            seed,
            can_downsample=True,
        )
        if fitted.rows is not None:
            feats = feats[fitted.rows.tolist()]
        best, excluded = choose(fitted.model, self._cfg)
        if best is None:
            summary = "; ".join(f"{e.candidate}: {e.reason}" for e in excluded) or "none fitted"
            return Unavailability(
                analysis="customer_segmentation",
                reason_code=ReasonCode.LOW_MODEL_QUALITY,
                message=f"No segmentation passed the quality and stability gates ({summary}).",
            )
        seg = self._cfg.segmentation
        metrics = Metrics(
            primary="silhouette",
            values={
                "silhouette": best.silhouette,
                "davies_bouldin": best.davies_bouldin,
                "stability_ari": best.stability,
                "noise_ratio": best.noise_ratio,
                "segments": float(len(set(best.labels[best.labels >= 0].tolist()))),
            },
        )
        selection = Selection(
            best=best.name,
            primary_metric="silhouette",
            scores={c.name: c.silhouette for c in fitted.model},
            excluded=excluded,
            margin_rule=(
                f"highest silhouette among candidates with silhouette >= {seg.min_silhouette}, "
                f"stability ARI >= {seg.min_stability_ari}, every cluster >= "
                f"{seg.min_cluster_share:.0%} of entities and noise <= {seg.max_noise_ratio:.0%}"
            ),
            fell_back_to_baseline=False,
            data_characteristics={"entities": feats.height, "features": list(columns)},
        )
        name = "hdbscan" if best.name == "hdbscan" else "kmeans"
        params: dict[str, JsonValue] = {"candidate": best.name, "features": list(columns)}
        if name == "kmeans":
            params |= {"k": int(best.name.removeprefix("kmeans_k")), "n_init": 10}
        model = ModelInfo(name=name, library=SKLEARN, params=params)
        run = self.persist(
            scope, version_id, MLTask.SEGMENTATION, model, args, metrics, seed, selection
        )
        limitations = [
            *fitted.caveats,
            "Segments describe similarity in the chosen features; they are not causal groups.",
        ]
        if best.noise_ratio > 0:
            limitations.append(f"{best.noise_ratio:.0%} of entities are unassigned (noise).")
        result = ModelResult(
            model_run_id=run.id,
            task=MLTask.SEGMENTATION,
            model=model,
            inputs=ModelInputs(dataset_version_id=version_id, rows=feats.height),
            training=TrainingInfo(split="none", seed=seed),
            metrics=metrics,
            limitations=limitations,
        )
        return SegmentsOutput(
            model_run_id=run.id,
            rows=feats.height,
            calculation=calculation(
                run, model, f"segmentation of {args.entity_column} ({best.name})"
            ),
            segments=profile(feats, columns, np.asarray(best.labels)),
            model_result=result,
            selection=selection,
        )
