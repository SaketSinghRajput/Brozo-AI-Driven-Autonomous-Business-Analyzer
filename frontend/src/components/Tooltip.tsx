import { cloneElement, useId, type ReactElement } from "react";

/**
 * Minimal shared tooltip (UI-UX §48: "Hover and focus; never the only
 * carrier of critical info"). Pure CSS (group-hover/group-focus-within) -
 * no JS state, works with keyboard focus as well as pointer hover.
 */
export function Tooltip({
  label,
  children,
}: {
  label: string;
  children: ReactElement<{ "aria-describedby"?: string }>;
}) {
  const id = useId();
  return (
    <span className="group relative inline-flex">
      {cloneElement(children, { "aria-describedby": id })}
      <span
        id={id}
        role="tooltip"
        className="text-caption pointer-events-none absolute top-full left-1/2 z-10 mt-1 w-max max-w-56 -translate-x-1/2 rounded-sm bg-text px-2 py-1 text-surface opacity-0 shadow-md transition-opacity duration-(--motion-duration-fast) group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {label}
      </span>
    </span>
  );
}
