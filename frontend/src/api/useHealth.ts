import { useEffect, useState } from "react";
import "./client";
import { healthApiV1HealthGet } from "./generated/sdk.gen";
import { isErrorEnvelope, type ErrorEnvelope } from "./errors";

export type HealthState =
  | { status: "loading" }
  | { status: "ok"; version: string }
  | { status: "error"; envelope: ErrorEnvelope }
  | { status: "unreachable" };

/**
 * Polls-once-on-mount backend liveness (GET /api/v1/health). A single call on load doesn't
 * justify pulling in TanStack Query (ADR-0012 adopts it for real data fetching later); this is
 * a tiny hook instead (CLAUDE.md section 21: fewer moving parts).
 */
export function useHealth(): HealthState {
  const [state, setState] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    healthApiV1HealthGet()
      .then(({ data, error }) => {
        if (cancelled) return;
        if (data) {
          setState({ status: "ok", version: data.version });
        } else if (isErrorEnvelope(error)) {
          setState({ status: "error", envelope: error });
        } else {
          setState({ status: "unreachable" });
        }
      })
      .catch(() => {
        if (!cancelled) setState({ status: "unreachable" });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
