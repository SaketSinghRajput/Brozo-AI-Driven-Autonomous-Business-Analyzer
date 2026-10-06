import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, ConfidenceLabel, ProvenanceChip, ValidationBadge } from "./StatusBadges";

describe("ConfidenceLabel", () => {
  it.each([
    ["high", "High confidence"],
    ["medium", "Moderate confidence"],
    ["low", "Low confidence"],
  ] as const)("shows %s as level text, never a percentage", (level, text) => {
    render(<ConfidenceLabel confidence={{ level, score: 0.42, basis: ["reason"] }} />);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/42/)).not.toBeInTheDocument();
  });

  it("always offers a Why? with the basis for low confidence", () => {
    render(<ConfidenceLabel confidence={{ level: "low", score: 0.2, basis: ["Few rows"] }} />);
    expect(screen.getByText("Why?")).toBeInTheDocument();
    expect(screen.getByText("Few rows")).toBeInTheDocument();
  });

  it("has no Why? for high confidence", () => {
    render(<ConfidenceLabel confidence={{ level: "high", score: 0.9, basis: ["x"] }} />);
    expect(screen.queryByText("Why?")).not.toBeInTheDocument();
  });
});

describe("Badge", () => {
  it("renders its text content", () => {
    render(<Badge variant="info">Degraded</Badge>);
    expect(screen.getByText("Degraded")).toBeInTheDocument();
  });
});

describe("ValidationBadge", () => {
  it("shows the VALID state as icon + text, not colour alone", () => {
    render(<ValidationBadge status="VALID" />);
    expect(screen.getByText("Validated")).toBeInTheDocument();
  });

  it("shows the REQUIRES_REVIEW state with its default explanation as a tooltip", () => {
    render(<ValidationBadge status="REQUIRES_REVIEW" />);
    const badge = screen.getByText("Needs review");
    expect(badge.closest("[title]")).toHaveAttribute(
      "title",
      "The available data does not fully support this conclusion.",
    );
  });

  it("shows the INVALID state text (only ever used in history/technical views)", () => {
    render(<ValidationBadge status="INVALID" />);
    expect(screen.getByText("Not shown")).toBeInTheDocument();
  });

  it("shows the UNCERTAIN state text", () => {
    render(<ValidationBadge status="UNCERTAIN" />);
    expect(screen.getByText("Unverified")).toBeInTheDocument();
  });
});

describe("ProvenanceChip", () => {
  it("renders the FACT kind label", () => {
    render(<ProvenanceChip kind="FACT" />);
    expect(screen.getByText("Source data")).toBeInTheDocument();
  });

  it("renders the SIMULATION kind label", () => {
    render(<ProvenanceChip kind="SIMULATION" />);
    expect(screen.getByText("Simulation")).toBeInTheDocument();
  });
});
