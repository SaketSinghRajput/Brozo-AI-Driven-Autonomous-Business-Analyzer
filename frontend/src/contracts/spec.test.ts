import { describe, expect, it } from 'vitest';
import { evidenceNumber, parseDashboardSpec } from './spec';
import { formatNumber } from '../lib/format';
import { retailFixture } from '../features/dashboard/fixtures/retail';

describe('parseDashboardSpec (runtime contract validation)', () => {
  it('accepts a contract-valid spec and resolves every widget', () => {
    const parsed = parseDashboardSpec(retailFixture.spec);
    expect(parsed.status).toBe('ok');
    if (parsed.status !== 'ok') return;
    expect(parsed.widgets.map((widget) => widget.kind)).toEqual([
      'kpi_card',
      'kpi_card',
      'kpi_card',
      'chart',
      'chart',
      'chart',
      'unavailable',
    ]);
  });

  it.each([null, 'spec', 42, [], { widgets: 'x' }])('rejects non-spec input %j', (input) => {
    expect(parseDashboardSpec(input).status).toBe('invalid');
  });

  it('asks for an upgrade on an unknown major version', () => {
    expect(parseDashboardSpec({ schema_version: '2.0', widgets: [] })).toEqual({
      status: 'upgrade_required',
      version: '2.0',
    });
  });

  it('rejects extra envelope properties (additionalProperties: false)', () => {
    const spec = { ...(retailFixture.spec as object), script: 'alert(1)' };
    expect(parseDashboardSpec(spec).status).toBe('invalid');
  });

  it('isolates a malformed chart widget and an unknown widget type', () => {
    const base = retailFixture.spec as { widgets: unknown[] };
    const parsed = parseDashboardSpec({
      ...base,
      widgets: [
        {
          id: 'w1',
          widget_type: 'line_chart',
          title: 't',
          validation_status: 'VALID',
          operation_id: 'o',
        },
        { id: 'w2', widget_type: 'sankey' },
        base.widgets[0],
      ],
    });
    expect(parsed.status).toBe('ok');
    if (parsed.status !== 'ok') return;
    expect(parsed.widgets[0].kind).toBe('invalid');
    expect(parsed.widgets[1]).toEqual({ kind: 'unknown', id: 'w2', widgetType: 'sankey' });
    expect(parsed.widgets[2].kind).toBe('kpi_card');
  });

  it('accepts schema_version 1.0 and 1.1', () => {
    for (const version of ['1.0', '1.1']) {
      const spec = { ...(retailFixture.spec as object), schema_version: version };
      expect(parseDashboardSpec(spec).status).toBe('ok');
    }
  });
});

describe('evidenceNumber (EvidenceRef.field as a dotted path)', () => {
  const record = { result: { value: 4.5, rows: 3 }, list: [{ n: 7 }], text: 'x' };
  it.each([
    ['result.value', 4.5],
    ['result.rows', 3],
    ['list.0.n', 7],
    ['text', null],
    ['result.missing', null],
    ['list.9.n', null],
    ['value', null],
  ])('%s -> %j', (field, expected) => {
    expect(evidenceNumber(record, field)).toBe(expected);
  });
});

describe('unit vocabulary (pct/pp are fractions)', () => {
  it.each([
    [0.142, { unit: 'pct', precision: 1 }, '14.2%'],
    [0.023, { unit: 'pp', precision: 1 }, '2.3 pp'],
    [1234.5, { unit: 'currency:INR', precision: 2 }, '₹1,234.50'],
    [99, { unit: 'currency:XXX', currency: 'USD', locale: 'en-US' }, '$99'],
    [1200, { unit: 'count' }, '1,200'],
    // Bare "currency" from a dataset with no ISO code: a plain number, no guessed symbol.
    // U-1: one grouping for the whole app (en-US, as the backend's text), whatever the spec says.
    [1318451.67, { unit: 'currency', precision: 2 }, '1,318,451.67'],
    [1318451.67, { unit: 'count', locale: 'en-IN' }, '1,318,452'],
    [5, { unit: 'currency', currency: 'EUR', locale: 'en-IE' }, '€5'],
    [27.1, { unit: 't/ha', precision: 1 }, '27.1 t/ha'],
  ])('%d %j -> %s', (value, format, expected) => {
    expect(formatNumber(value, format)).toBe(expected);
  });
});
