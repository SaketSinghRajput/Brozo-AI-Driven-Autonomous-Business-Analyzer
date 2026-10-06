import { useState, type ReactNode } from 'react';

const PREFIX = 'first-use-tip:';

function seen(id: string): boolean {
  try {
    return window.localStorage.getItem(PREFIX + id) === 'dismissed';
  } catch {
    return false; // storage blocked: show the tip; dismissal then lasts for this page only
  }
}

/**
 * One-time, dismissible hint (UI-UX §12: once per feature, never blocks interaction).
 * Remembered per browser in localStorage (a per-viewer convenience, not user data).
 */
export function FirstUseTip({ id, children }: { id: string; children: ReactNode }) {
  const [hidden, setHidden] = useState(() => seen(id));
  if (hidden) return null;

  function dismiss() {
    setHidden(true);
    try {
      window.localStorage.setItem(PREFIX + id, 'dismissed');
    } catch {
      // Storage unavailable (private mode / blocked): hidden until the next page load.
    }
  }

  return (
    <aside
      aria-label="Tip"
      className="text-caption flex items-start gap-2 rounded-md border border-info/40 bg-info/5 p-3 text-text"
    >
      <span aria-hidden="true" className="text-info">
        ⓘ
      </span>
      <div className="flex-1">{children}</div>
      <button
        type="button"
        onClick={dismiss}
        className="rounded-sm px-2 py-0.5 text-text-muted hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-focus"
      >
        Got it
      </button>
    </aside>
  );
}
