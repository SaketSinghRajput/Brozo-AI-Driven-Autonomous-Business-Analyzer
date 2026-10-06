import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * CLAUDE.md §5 ROLE 5 / design.md §55: no component contains domain-specific logic or words.
 * Domain appears only as data (fixtures, API). Fixtures, the demo page and tests are excluded.
 */
const src = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCANNED = [
  'components',
  'features/dashboard',
  'features/evidence',
  'lib',
  'contracts/spec.ts',
  'app',
  'context',
  'pages',
  'features/datasets',
  'features/notifications',
  'features/onboarding',
  'features/chat',
  'features/semantic',
];
const EXCLUDED = /(fixtures|demo|generated)[\\/]|\.test\.tsx?$/;
const DOMAIN_TERMS =
  /\b(retail|e-?commerce|saas|agricultur\w*|revenue|mrr|arr|churn|crop|harvest|inventory|subscription|customer|farm)\b/i;

function files(path: string): string[] {
  const full = join(src, path);
  if (/\.tsx?$/.test(path)) return [full];
  return readdirSync(full, { recursive: true, encoding: 'utf-8' })
    .filter((name) => /\.tsx?$/.test(name))
    .map((name) => join(full, name));
}

describe('no domain hardcoding in rendering code', () => {
  const scanned = SCANNED.flatMap(files).filter((file) => !EXCLUDED.test(file));

  it('scans the dashboard components', () => {
    expect(scanned.some((file) => file.endsWith('DashboardRenderer.tsx'))).toBe(true);
    expect(scanned.some((file) => file.endsWith('KpiWidget.tsx'))).toBe(true);
  });

  it.each(scanned.map((file) => [file.slice(src.length + 1)]))('%s has no domain terms', (file) => {
    const match = DOMAIN_TERMS.exec(readFileSync(join(src, file), 'utf-8'));
    expect(match?.[0] ?? null).toBeNull();
  });
});
