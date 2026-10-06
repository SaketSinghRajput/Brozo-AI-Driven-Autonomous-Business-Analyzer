"""Reproducible demo against a running stack (DEMO-001/002/003).

    DEMO_EMAIL=admin@demo.local DEMO_PASSWORD=... \
        uv run --project backend python scripts/demo/run_demo.py --out docs/demo/transcripts/x.md

Uses only the public HTTP API through the proxy, exactly like the browser: login (session
cookie + CSRF header), upload fixtures, poll the version until READY, read understanding /
capabilities / dashboard / evidence, and run chat turns over SSE. Prints a concise transcript
with evidence ids and validation statuses. The password, session cookie and CSRF token are never
printed or written.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures"
TERMINAL = {"READY", "VALIDATION_FAILED", "INGESTION_FAILED", "ANALYSIS_FAILED"}


class Demo:
    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=60)
        self.lines: list[str] = []
        response = self.http.post("/auth/login", json={"email": email, "password": password})
        if response.status_code != 200:
            sys.exit(f"login failed: HTTP {response.status_code}")
        session = response.json()
        self.csrf = {"X-CSRF-Token": session["csrf_token"]}  # kept in memory only
        self.say(
            f"Signed in as {session['email']} ({session['role']}) in {session['workspace_id']}"
        )

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def get(self, path: str) -> Any:
        response = self.http.get(path)
        response.raise_for_status()
        return response.json()

    # ---- datasets -------------------------------------------------------------------------
    def upload(self, relative: str) -> tuple[str, str]:
        path = FIXTURES / relative
        with path.open("rb") as handle:
            response = self.http.post(
                "/datasets", headers=self.csrf, files={"file": (path.name, handle, "text/csv")}
            )
        response.raise_for_status()
        body = response.json()
        fresh = "new version" if response.status_code == 201 else "identical file, existing version"
        self.say(f"Upload {relative}: {body['dataset_id']} / {body['version_id']} ({fresh})")
        return body["dataset_id"], body["version_id"]

    def wait_ready(self, dataset_id: str, version_id: str, timeout_s: float = 300) -> str:
        seen: list[str] = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = self.get(f"/datasets/{dataset_id}/versions/{version_id}")["state"]
            if not seen or seen[-1] != state:
                seen.append(state)
            if state in TERMINAL:
                break
            time.sleep(1)
        self.say(f"  states observed: {' -> '.join(seen)}")
        return seen[-1]

    def understand(self, dataset_id: str, version_id: str) -> None:
        base = f"/datasets/{dataset_id}/versions/{version_id}"
        understanding = self.get(f"{base}/understanding")
        domain = understanding["domain"]
        self.say(
            f"  domain: {domain['primary']} ({domain['status']}, confidence {domain['confidence']},"
            f" source {understanding['domain_source']})"
        )
        model = self.get(f"{base}/semantic-model")
        self.say(f"  semantic model v{model['version']}:")
        self.say(f"    entities: {', '.join(e['name'] for e in model['entities'])}")
        self.say(f"    measures: {', '.join(m['name'] for m in model['measures'])}")
        self.say(f"    metrics: {', '.join(m['name'] for m in model['metrics'])}")
        capabilities = self.get(f"{base}/capabilities")
        self.say(f"  available analyses: {', '.join(capabilities['available'])}")
        for item in capabilities["unavailable"]:
            self.say(
                f"  NOT available: {item['analysis']} [{item['reason_code']}] {item['message']}"
            )

    def evidence(self, evidence_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.get(f"/evidence/{evidence_id}")
        return result

    def dashboard(self, dataset_id: str, version_id: str, forecast_rows: int = 0) -> None:
        spec = self.get(f"/datasets/{dataset_id}/versions/{version_id}/dashboard")
        self.say(f"  dashboard {spec['dashboard_id']} (stale={spec['stale']}):")
        for sentence in spec["summary"]:
            ids = ", ".join(sentence["evidence_ids"])
            self.say(f"    summary [{sentence['validation_status']}] {sentence['text']} ({ids})")
        for widget in spec["widgets"]:
            kind = widget["widget_type"]
            status = widget.get("validation_status", "-")
            if kind == "kpi_card" and widget.get("value_ref"):
                ref = widget["value_ref"]["evidence_id"]
                value = self.evidence(ref)["result"].get("value")
                self.say(f"    KPI {widget['title']} = {value} [{status}] ({ref})")
            elif kind == "unavailable":
                self.say(
                    f"    unavailable {widget['title']} [{widget['reason_code']}]"
                    f" {widget['message']}"
                )
            else:  # line_chart, bar_chart, table, ...
                self.say(f"    {kind} {widget['title']} [{status}]")
                for overlay in (widget.get("chart") or {}).get("overlays") or []:
                    self.overlay(overlay, forecast_rows)
        for insight in spec["insights"]:
            self.say(f"    insight [{insight['validation_status']}] {insight['observed']['text']}")
            for explanation in insight["possible_explanations"]:
                self.say(f"      - {explanation['text']}")

    def overlay(self, overlay: dict[str, Any], rows: int) -> None:
        evidence = self.evidence(overlay["evidence_id"])
        model = evidence.get("model") or {}
        self.say(
            f"      overlay {overlay['type']} ({overlay['evidence_id']}): model"
            f" {model.get('model_id')} v{model.get('model_version')}"
        )
        if rows and overlay["type"] == "forecast_band":
            data = self.get(f"/evidence/{overlay['evidence_id']}/data")
            for row in (data.get("rows") or [])[:rows]:
                self.say(f"        {json.dumps(row, default=str)}")

    # ---- chat -----------------------------------------------------------------------------
    def conversation(self, dataset_id: str, version_id: str) -> str:
        model = self.get(f"/datasets/{dataset_id}/versions/{version_id}/semantic-model")
        response = self.http.post(
            "/chat/conversations",
            headers=self.csrf,
            json={
                "dataset_version_id": version_id,
                "semantic_model_version": model["version"],
                "title": "Demo",
            },
        )
        response.raise_for_status()
        conversation_id: str = response.json()["id"]
        return conversation_id

    def events(self, conversation_id: str, question: str) -> Iterator[dict[str, Any]]:
        with self.http.stream(
            "POST",
            f"/chat/conversations/{conversation_id}/messages",
            headers=self.csrf,
            json={"content": question},
            timeout=httpx.Timeout(30, read=300),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data:"):
                    yield json.loads(line[5:])["event"]

    def ask(self, conversation_id: str, question: str) -> None:
        self.say(f"Q: {question}")
        started = time.monotonic()
        answer: list[str] = []
        caveats: list[str] = []
        for event in self.events(conversation_id, question):
            kind = event["type"]
            if kind == "tool_call":
                findings = event.get("findings") or []
                self.say(
                    f"  tool {event['name']}({event.get('args_summary', '')}) -> {event['status']}"
                    f" {event.get('evidence_id') or event.get('error_code') or ''}".rstrip()
                )
                for finding in findings[:5]:
                    self.say(
                        f"    factor {finding['factor']}: {finding['change']}"
                        f" ({finding['relation_type']})"
                    )
            elif kind == "segment":
                segment = event["segment"]
                if segment["type"] == "number":
                    answer.append(f"[{segment['value']} <{segment['evidence_id']}>]")
                elif segment["type"] == "caveat":
                    caveats.append(segment["text"])
                else:
                    answer.append(segment["text"])
            elif kind == "validation":
                issues = ", ".join(i["code"] for i in event.get("issues") or []) or "none"
                level = (event.get("confidence") or {}).get("level")
                self.say(f"  validation: {event['status']} (confidence {level}; issues: {issues})")
            elif kind == "final":
                refusal = event.get("refusal")
                if refusal:
                    self.say(f"  refusal: {refusal['code']} {refusal['message']}")
                self.say(
                    f"  final: validation_status={event.get('validation_status')}"
                    f" mode={event.get('mode')} ({time.monotonic() - started:.0f} s)"
                )
            elif kind == "error":
                self.say(f"  error: {event['error']['error_code']} {event['error']['message']}")
        self.say(f"A: {''.join(answer)}")
        for caveat in caveats:
            self.say(f"  caveat: {caveat}")
        self.say()


def scenario_1(demo: Demo) -> tuple[str, str]:
    demo.say("## DEMO-001 unknown CSV -> understand -> domain -> semantics -> analyze -> dashboard")
    result = ("", "")
    for relative in ["retail/orders.csv", "agriculture/production.csv", "insufficient/sales.csv"]:
        demo.say()
        dataset_id, version_id = demo.upload(relative)
        if demo.wait_ready(dataset_id, version_id) != "READY":
            continue
        demo.understand(dataset_id, version_id)
        demo.dashboard(dataset_id, version_id)
        if relative == "retail/orders.csv":
            result = (dataset_id, version_id)
    return result


def scenario_2(demo: Demo, dataset_id: str, version_id: str) -> None:
    demo.say(
        "\n## DEMO-002 question -> plan -> SQL/analytics -> evidence -> validation -> answer\n"
    )
    conversation_id = demo.conversation(dataset_id, version_id)
    demo.ask(conversation_id, "What was revenue in North in August 2026?")
    demo.ask(conversation_id, "Why did revenue drop in North in August 2026?")


def scenario_3(demo: Demo, dataset_id: str, version_id: str) -> None:
    demo.say("\n## DEMO-003 ML: forecast and anomaly (dashboard overlays + chat)\n")
    demo.dashboard(dataset_id, version_id, forecast_rows=3)
    demo.say()
    conversation_id = demo.conversation(dataset_id, version_id)
    demo.ask(conversation_id, "Forecast revenue for the next 3 months.")
    demo.ask(conversation_id, "Were there any unusual months for revenue?")


def scenario_isolation(demo: Demo, dataset_id: str, version_id: str, base_url: str) -> None:
    """DEMO-008a: a user of another workspace asks for this workspace's resources by id."""
    email, password = os.environ.get("DEMO_B_EMAIL"), os.environ.get("DEMO_B_PASSWORD")
    if not email or not password:
        sys.exit("scenario 8 needs DEMO_B_EMAIL / DEMO_B_PASSWORD (a user of another workspace)")
    demo.say("\n## DEMO-008a workspace isolation through the proxy\n")
    spec = demo.get(f"/datasets/{dataset_id}/versions/{version_id}/dashboard")
    evidence_id = spec["summary"][0]["evidence_ids"][0]
    other = Demo(base_url, email, password)
    demo.lines.append(other.lines[-1])  # already printed by `other`
    base = f"/datasets/{dataset_id}/versions/{version_id}"
    for path in [
        f"/datasets/{dataset_id}",
        base,
        f"{base}/dashboard",
        f"{base}/semantic-model",
        f"/evidence/{evidence_id}",
        f"/evidence/{evidence_id}/data",
    ]:
        status = other.http.get(path).status_code
        verdict = "denied" if status in (403, 404) else "LEAK"
        demo.say(f"  workspace B GET {path} -> {status} ({verdict})")
    listed = [d["id"] for d in other.get("/datasets")]
    demo.say(f"  workspace B dataset list contains A's dataset: {dataset_id in listed}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--base-url", default=os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument("--scenarios", default="1,2,3", help="comma list of 1,2,3,8")
    parser.add_argument("--out", type=Path, help="also write the transcript (markdown) here")
    args = parser.parse_args()
    email, password = os.environ.get("DEMO_EMAIL"), os.environ.get("DEMO_PASSWORD")
    if not email or not password:
        sys.exit("set DEMO_EMAIL and DEMO_PASSWORD")

    demo = Demo(args.base_url, email, password)
    wanted = set(args.scenarios.split(","))
    if "1" in wanted:
        dataset_id, version_id = scenario_1(demo)
    else:
        dataset_id, version_id = demo.upload("retail/orders.csv")
        demo.wait_ready(dataset_id, version_id)
    if "2" in wanted:
        scenario_2(demo, dataset_id, version_id)
    if "3" in wanted:
        scenario_3(demo, dataset_id, version_id)
    if "8" in wanted:
        scenario_isolation(demo, dataset_id, version_id, args.base_url)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# run_demo.py transcript ({time.strftime('%Y-%m-%d %H:%M')}, {args.base_url})\n\n"
        )
        body = "```text\n" + "\n".join(demo.lines) + "\n```\n"
        args.out.write_text(header + body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
