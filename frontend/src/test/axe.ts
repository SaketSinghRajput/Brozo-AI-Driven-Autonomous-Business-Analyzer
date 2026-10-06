import axe from 'axe-core';

/**
 * Automated WCAG checks (axe-core, TASK-UI-024) over the rendered DOM. Returns one line per
 * violation so a failing assertion prints what to fix. Colour contrast is off here: jsdom has no
 * layout or computed colours (axe reports it "incomplete"); token contrast is checked by
 * src/test/contrast.test.ts instead.
 */
export async function axeViolations(root: Element = document.body): Promise<string[]> {
  const results = await axe.run(root, {
    resultTypes: ['violations'],
    rules: { 'color-contrast': { enabled: false } },
  });
  return results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'n/a'}): ${violation.help} -> ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  );
}
