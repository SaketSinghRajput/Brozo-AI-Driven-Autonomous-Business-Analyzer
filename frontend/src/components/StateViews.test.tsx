import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmptyState, ErrorState, InsufficientDataState } from "./StateViews";

describe("EmptyState", () => {
  it("renders the message and primary action", async () => {
    const onAction = vi.fn();
    const user = userEvent.setup();
    render(
      <EmptyState
        message="No datasets yet. Upload your first business dataset to begin automatic analysis."
        actionLabel="Upload dataset"
        onAction={onAction}
      />,
    );

    expect(
      screen.getByText(/No datasets yet\. Upload your first business dataset/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Upload dataset" }));
    expect(onAction).toHaveBeenCalledOnce();
  });
});

describe("ErrorState", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the TRD §35 envelope fields and offers a retry button when retryable", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(
      <ErrorState
        message="The file could not be read as CSV."
        hint="Save the sheet as UTF-8 CSV or upload the .xlsx file directly."
        retryable
        onRetry={onRetry}
        traceId="4f1c2a"
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("The file could not be read as CSV.");
    expect(screen.getByText(/Save the sheet as UTF-8 CSV/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
    expect(screen.getByText("Trace ID: 4f1c2a")).toBeInTheDocument();
  });

  it("copies the trace ID to the clipboard when clicked", async () => {
    // jsdom 30 ships a real (unimplemented) navigator.clipboard EventTarget,
    // so spy on its writeText directly rather than replacing `navigator`.
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<ErrorState message="Unexpected error." traceId="abc-123" />);
    await user.click(screen.getByText("Trace ID: abc-123"));

    expect(writeText).toHaveBeenCalledWith("abc-123");
  });
});

describe("InsufficientDataState", () => {
  it("renders as a helpful answer with the reason and suggested actions", async () => {
    const onAction = vi.fn();
    const user = userEvent.setup();
    render(
      <InsufficientDataState
        reason="The dataset doesn't contain customer subscription or cancellation dates, which churn analysis needs."
        actions={[
          { label: "Analyze repeat purchases instead", actionLabel: "Run", onAction },
        ]}
      />,
    );

    expect(
      screen.getByText("I can't reliably answer this from the available data."),
    ).toBeInTheDocument();
    expect(screen.getByText(/churn analysis needs/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(onAction).toHaveBeenCalledOnce();
  });
});
