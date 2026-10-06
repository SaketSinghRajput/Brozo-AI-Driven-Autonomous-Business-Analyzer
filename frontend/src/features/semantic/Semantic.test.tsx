import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Route } from '../../test/mockApi';
import { HEALTH_OK, envelope, json, mockApi, session, versionBody } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';

const DATASET = {
  id: 'ds_1',
  name: 'Export',
  workspace_id: 'ws_abc123',
  source_type: 'file_upload',
  created_at: '2026-09-28T10:00:00Z',
};

function understanding(domain: object | null, flags: string[] = []) {
  return {
    understanding: {
      dataset_version_id: 'dsv_1',
      engine_version: 'test',
      columns: [],
      dimensions: [],
      entities: [],
      measures: [],
      relationships: [],
      review_flags: flags,
      time_dimensions: [],
    },
    domain,
    domain_source: domain ? 'detector' : null,
    review_flags: flags,
  };
}

const AMBIGUOUS = {
  status: 'AMBIGUOUS',
  primary: null,
  confidence: 0.4,
  pack_versions: {},
  candidates: [
    {
      domain: 'pack_a',
      score: 0.41,
      evidence: [{ kind: 'lexicon', detail: 'Column names match pack A terms', weight: 0.3 }],
    },
    { domain: 'pack_b', score: 0.39, evidence: [] },
  ],
};

function model(version: number, overrides: object[] = []) {
  return {
    semantic_model_id: 'sm_1',
    dataset_version_id: 'dsv_1',
    version,
    measures: [
      {
        name: 'amount',
        column: 'amt',
        additivity: 'additive',
        confidence: 0.8,
        default_agg: 'sum',
        unit: null,
      },
    ],
    dimensions: [{ name: 'group', column: 'grp', type: 'category' }],
    entities: [],
    metrics: [{ name: 'total_amount', expr: 'sum(amount)' }],
    overrides,
  };
}

const CAPABILITIES = {
  dataset_version_id: 'dsv_1',
  semantic_model_version: 3,
  rules_version: '1',
  available: ['trend_analysis'],
  unavailable: [
    {
      analysis: 'forecast',
      reason_code: 'INSUFFICIENT_HISTORY',
      message: 'Needs at least 24 periods; the data has 6.',
      would_enable: 'More history',
    },
  ],
};

const BASE = '/api/v1/datasets/ds_1/versions/dsv_1';

function setup(role: 'viewer' | 'analyst', extra: Record<string, Route> = {}) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session(role) },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets/ds_1': { body: DATASET },
    'GET /api/v1/datasets/ds_1/versions': {
      body: [versionBody('SEMANTICALLY_MAPPED', { row_count: 10 })],
    },
    [`GET ${BASE}/understanding`]: { body: understanding(AMBIGUOUS, ['MAPPING_AMBIGUOUS:amt']) },
    [`GET ${BASE}/semantic-model`]: { body: model(3) },
    [`GET ${BASE}/capabilities`]: { body: CAPABILITIES },
    ...extra,
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('SemanticMappingViewer (TASK-UI-007)', () => {
  it('explains an ambiguous domain and shows mappings read-only to a viewer', async () => {
    setup('viewer');
    renderApp('/datasets/ds_1/understanding');

    expect(
      await screen.findByText(/We could not confidently identify the domain — generic analysis/),
    ).toBeInTheDocument();
    expect(screen.getByText('Column names match pack A terms')).toBeInTheDocument();
    const table = await screen.findByRole('table');
    expect(within(table).getByText('amt')).toBeInTheDocument();
    expect(within(table).getByText('⚑ Needs review')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Use Pack/ })).not.toBeInTheDocument();
    expect(screen.getAllByText(/Requires Analyst role/)).toHaveLength(2);
  });

  it('confirms a mapping with a PATCH against the model version on screen', async () => {
    const confirmed = model(4, [
      { field: 'mapping.amt', value: 'amount', by: 'usr_1', at: '2026-09-28T10:00:00Z' },
    ]);
    const { calls } = setup('analyst', { [`PATCH ${BASE}/semantic-model`]: { body: confirmed } });
    const user = userEvent.setup();
    renderApp('/datasets/ds_1/understanding');

    await user.click(await screen.findByRole('button', { name: 'Confirm amt means amount' }));
    expect(await screen.findByText(/Saved as semantic model v4/)).toBeInTheDocument();
    const patch = calls.find((c) => c.method === 'PATCH');
    expect(await patch?.clone().json()).toEqual({
      base_version: 3,
      overrides: [{ field: 'mapping.amt', value: 'amount' }],
    });
    expect(patch?.headers.get('X-CSRF-Token')).toBe('csrf-token-value');
    expect(screen.getByText('Your decision')).toBeInTheDocument();
  });

  it('asks the user to reload when the model changed underneath (409)', async () => {
    let gets = 0;
    const conflict = envelope(409, 'STATE_CONFLICT', 'base_version 3 is not the current version 4');
    setup('analyst', {
      [`GET ${BASE}/semantic-model`]: () => json(model(3 + gets++)),
      [`PATCH ${BASE}/semantic-model`]: conflict,
    });
    const user = userEvent.setup();
    renderApp('/datasets/ds_1/understanding');

    await user.click(await screen.findByRole('button', { name: 'Edit amt' }));
    await user.type(screen.getByLabelText('Unit'), 'kg');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('changed since you opened it');

    await user.click(within(alert).getByRole('button', { name: 'Reload' }));
    expect(await screen.findByText('Semantic model v4')).toBeInTheDocument();
    expect(screen.queryByText(/changed since you opened it/)).not.toBeInTheDocument();
  });

  it('lets an analyst choose the domain among the candidates', async () => {
    const chosen = { ...AMBIGUOUS, status: 'KNOWN', primary: 'pack_b' };
    const { calls } = setup('analyst', {
      'PUT /api/v1/datasets/ds_1/domain': {
        body: { version_id: 'dsv_1', domain: chosen, semantic_model_version: 4 },
      },
    });
    const user = userEvent.setup();
    renderApp('/datasets/ds_1/understanding');

    await user.click(await screen.findByRole('button', { name: 'Use Pack b' }));
    expect(await screen.findByText('Saved. Updating analyses…')).toBeInTheDocument();
    const put = calls.find((c) => c.method === 'PUT');
    expect(await put?.clone().json()).toEqual({ domain: 'pack_b' });
  });

  it('shows what the data supports on the overview', async () => {
    setup('viewer');
    renderApp('/datasets/ds_1/overview');

    expect(await screen.findByText('Analyses for this data')).toBeInTheDocument();
    expect(screen.getByText('Needs at least 24 periods; the data has 6.')).toBeInTheDocument();
  });

  it('shows the detected domain and its confidence level on the overview (U-3)', async () => {
    const known = { ...AMBIGUOUS, status: 'KNOWN', primary: 'pack_a', confidence: 0.86 };
    setup('viewer', {
      [`GET ${BASE}/understanding`]: {
        body: { ...understanding(known), domain_confidence_level: 'high' },
      },
    });
    renderApp('/datasets/ds_1/overview');

    const card = (await screen.findByText('Domain')).closest('section');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('Pack a');
    expect(card?.textContent).toContain('High confidence');
  });
});
