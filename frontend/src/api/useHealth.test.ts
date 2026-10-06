import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useHealth } from "./useHealth";

describe("useHealth", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("reports ok with the backend version", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { result } = renderHook(() => useHealth());

    expect(result.current).toEqual({ status: "loading" });
    await waitFor(() => expect(result.current.status).toBe("ok"));
    expect(result.current).toEqual({ status: "ok", version: "0.1.0" });
  });

  it("reports unreachable when the network request fails", async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useHealth());

    await waitFor(() => expect(result.current.status).toBe("unreachable"));
  });

  it("reports the error envelope when the backend returns one", async () => {
    const envelope = {
      error_code: "SYSTEM_ERROR",
      message: "An unexpected error occurred.",
      details: null,
      retryable: false,
      trace_id: "abc123",
      timestamp: "2026-09-28T00:00:00Z",
    };
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(envelope), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { result } = renderHook(() => useHealth());

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current).toEqual({ status: "error", envelope });
  });
});
