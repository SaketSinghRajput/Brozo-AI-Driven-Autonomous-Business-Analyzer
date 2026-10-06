import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HEALTH_OK, mockApi, session } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';
import { FirstUseTip } from './FirstUseTip';

const MANIFEST = [{ file: 'sample-a.csv', title: 'Sample A', description: 'Synthetic rows.' }];

function api(role: 'viewer' | 'analyst', datasets: unknown[] = []) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session(role) },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets': { body: datasets },
    'GET /samples/index.json': { body: MANIFEST },
    'GET /samples/sample-a.csv': () => new Response('a,b\n1,2\n', { status: 200 }),
  });
}

describe('Welcome and sample data (TASK-UI-026)', () => {
  beforeEach(() => window.localStorage.clear());

  it('welcomes a first-time analyst with the privacy note and both ways to start', async () => {
    api('analyst');
    renderApp('/');

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent to external AI services/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Upload dataset' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Try with sample data' })).toHaveAttribute(
      'href',
      '/datasets/upload#samples',
    );
  });

  it('does not offer upload or samples to a viewer', async () => {
    api('viewer');
    renderApp('/');

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload dataset' })).toBeDisabled();
    expect(screen.queryByRole('link', { name: 'Try with sample data' })).not.toBeInTheDocument();
  });

  it('loads a bundled sample into the uploader', async () => {
    api('analyst');
    const user = userEvent.setup();
    renderApp('/datasets/upload');

    await user.click(await screen.findByRole('button', { name: 'Use sample: Sample A' }));

    expect(await screen.findByText('sample-a.csv')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled();
  });

  it('shows recent datasets instead of the welcome once data exists', async () => {
    api('analyst', [
      {
        id: 'ds_1',
        name: 'First export',
        workspace_id: 'ws_abc123',
        source_type: 'file_upload',
        created_at: '2026-09-28T10:00:00Z',
      },
    ]);
    renderApp('/');

    expect(await screen.findByRole('link', { name: 'First export' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Welcome' })).not.toBeInTheDocument();
  });
});

describe('FirstUseTip', () => {
  beforeEach(() => window.localStorage.clear());

  it('stays dismissed once dismissed', async () => {
    const user = userEvent.setup();
    const first = render(<FirstUseTip id="t">Hint text</FirstUseTip>);
    await user.click(screen.getByRole('button', { name: 'Got it' }));
    expect(screen.queryByText('Hint text')).not.toBeInTheDocument();
    first.unmount();

    render(<FirstUseTip id="t">Hint text</FirstUseTip>);
    expect(screen.queryByText('Hint text')).not.toBeInTheDocument();
  });
});
