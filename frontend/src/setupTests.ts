import { afterEach } from "vitest";
import { cleanup, configure } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

// vitest.config test.globals is false, so Testing Library's own automatic
// cleanup (which relies on a global `afterEach`) never registers. Do it
// explicitly here instead - one place, every test file benefits.
afterEach(() => {
  cleanup();
});

// Full route-tree renders (auth check -> shell -> page queries) can exceed the default 1s
// findBy* window when the whole suite runs in parallel on the dev host.
// Dashboard renders (spec validation + evidence + /data rows) took ~12 s under full parallel load,
// so the window stays well inside the 20 s testTimeout.
configure({ asyncUtilTimeout: 10000 });

// jsdom gaps used by the dashboard: <dialog> modal API and ResizeObserver (Recharts
// ResponsiveContainer). Minimal stand-ins; real browsers provide both.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
