import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { HEALTH_OK, mockApi, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';

function renderAs(role: 'viewer' | 'analyst') {
  mockApi({ 'GET /api/v1/auth/me': { body: session(role) }, 'GET /api/v1/health': HEALTH_OK });
  renderApp('/privacy');
}

describe('Privacy & system info (TASK-UI-031)', () => {
  it('states local-only processing, storage and no telemetry, with the model for analysts', async () => {
    renderAs('analyst');

    expect(
      await screen.findByRole('heading', { name: 'Privacy & system info' }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/No external AI services are used/).length).toBeGreaterThan(0);
    expect(screen.getByText(/PostgreSQL database/)).toBeInTheDocument();
    expect(screen.getByText(/sends no telemetry/)).toBeInTheDocument();
    expect(screen.getByText('qwen2.5:7b, 4-bit quantized (Q4_K_M)')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/100% secure|completely private/i);
  });

  it('hides model names from viewers', async () => {
    renderAs('viewer');

    expect(
      await screen.findByText('Model names are shown to Analysts and administrators.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/qwen2\.5/)).not.toBeInTheDocument();
  });
});
