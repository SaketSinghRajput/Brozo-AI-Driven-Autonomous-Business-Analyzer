import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * Automated WCAG contrast check (TASK-UI-025, UI-UX §46/§53). Parses the
 * actual token values out of src/styles/tokens.css - it does not duplicate
 * them here - so this test fails the moment the CSS drifts from the
 * required ratios.
 */

const here = dirname(fileURLToPath(import.meta.url));
const tokensPath = resolve(here, '../styles/tokens.css');
const tokensCss = readFileSync(tokensPath, 'utf-8');

function extractBlock(css: string, selectorPattern: RegExp): string {
  const match = selectorPattern.exec(css);
  if (!match) {
    throw new Error(`Could not find a block matching ${selectorPattern} in tokens.css`);
  }
  const openBraceIndex = match.index + match[0].length - 1;
  let depth = 0;
  for (let i = openBraceIndex; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}') {
      depth -= 1;
      if (depth === 0) {
        return css.slice(openBraceIndex + 1, i);
      }
    }
  }
  throw new Error(`Unbalanced braces while reading block for ${selectorPattern}`);
}

function extractColorTokens(block: string): Record<string, string> {
  const tokens: Record<string, string> = {};
  const re = /--color-([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(block))) {
    tokens[match[1]] = match[2];
  }
  return tokens;
}

const lightBlock = extractBlock(tokensCss, /@theme\s*\{/);
const darkBlock = extractBlock(tokensCss, /:root\[data-theme=['"]dark['"]\]\s*\{/);

const light = extractColorTokens(lightBlock);
const dark = extractColorTokens(darkBlock);

// Sanity check: both themes define the full token set the assertions below need.
const requiredTokens = [
  'bg',
  'surface',
  'text',
  'text-muted',
  'border',
  'primary',
  'success',
  'warning',
  'error',
  'info',
  'focus',
];

for (const name of requiredTokens) {
  if (!(name in light)) throw new Error(`Light theme is missing --color-${name}`);
  if (!(name in dark)) throw new Error(`Dark theme is missing --color-${name}`);
}

function hexToRgb(hex: string): [number, number, number] {
  const value = hex.replace('#', '');
  const n = Number.parseInt(value, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** WCAG 2.x relative luminance, https://www.w3.org/TR/WCAG21/#dfn-relative-luminance */
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const channel = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const [rl, gl, bl] = [channel(r), channel(g), channel(b)];
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl;
}

/** WCAG 2.x contrast ratio, https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio */
function contrastRatio(hexA: string, hexB: string): number {
  const lumA = relativeLuminance(hexToRgb(hexA));
  const lumB = relativeLuminance(hexToRgb(hexB));
  const lighter = Math.max(lumA, lumB);
  const darker = Math.min(lumA, lumB);
  return (lighter + 0.05) / (darker + 0.05);
}

const TEXT_MIN = 4.5;
const UI_MIN = 3;

describe.each([
  ['light', light],
  ['dark', dark],
])('%s theme contrast (WCAG 2.2 AA)', (_name, tokens) => {
  it(`text vs bg >= ${TEXT_MIN}:1`, () => {
    expect(contrastRatio(tokens.text, tokens.bg)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it(`text vs surface >= ${TEXT_MIN}:1`, () => {
    expect(contrastRatio(tokens.text, tokens.surface)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it(`text-muted vs bg >= ${TEXT_MIN}:1`, () => {
    expect(contrastRatio(tokens['text-muted'], tokens.bg)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it(`text-muted vs surface >= ${TEXT_MIN}:1`, () => {
    expect(contrastRatio(tokens['text-muted'], tokens.surface)).toBeGreaterThanOrEqual(TEXT_MIN);
  });

  it.each(['success', 'warning', 'error', 'info'])(
    `status colour "%s" vs surface >= ${UI_MIN}:1`,
    (status) => {
      expect(contrastRatio(tokens[status], tokens.surface)).toBeGreaterThanOrEqual(UI_MIN);
    },
  );

  it(`border vs surface >= ${UI_MIN}:1`, () => {
    expect(contrastRatio(tokens.border, tokens.surface)).toBeGreaterThanOrEqual(UI_MIN);
  });

  it(`focus vs surface >= ${UI_MIN}:1`, () => {
    expect(contrastRatio(tokens.focus, tokens.surface)).toBeGreaterThanOrEqual(UI_MIN);
  });
});
