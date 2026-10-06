import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  HEALTH_OK,
  envelope,
  json,
  mockApi,
  session,
  versionBody,
  type Route,
} from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';
import { MAX_UPLOAD_BYTES } from './DatasetUploader';

interface Reply {
  status: number;
  body: unknown;
}

/** Minimal XMLHttpRequest stand-in: records the request, reports progress, then replies. */
class FakeXhr {
  static last: FakeXhr | null = null;
  static reply: Reply = { status: 500, body: null };
  method = '';
  url = '';
  headers: Record<string, string> = {};
  body: FormData | null = null;
  status = 0;
  responseText = '';
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: FormData) {
    FakeXhr.last = this;
    this.body = body;
    setTimeout(() => {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent);
      this.status = FakeXhr.reply.status;
      this.responseText = JSON.stringify(FakeXhr.reply.body);
      this.onload?.();
    }, 0);
  }
  abort() {
    this.onabort?.();
  }
}

const UPLOADED = {
  dataset_id: 'ds_1',
  version_id: 'dsv_1',
  job_id: 'job_1',
  status: 'UPLOADED',
};

function apiFor(role: 'viewer' | 'analyst', extra: Record<string, Route> = {}) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session(role) },
    'GET /api/v1/health': HEALTH_OK,
    ...extra,
  });
}

async function pickFile(file: File) {
  const user = userEvent.setup();
  await user.upload(await screen.findByLabelText('Choose a file'), file);
  return user;
}

describe('DatasetUploader (TASK-UI-004)', () => {
  beforeEach(() => {
    FakeXhr.last = null;
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
  });

  it.each([
    [400, 'SECURITY_ERROR', 'MACRO_ENABLED', "macro-enabled workbooks aren't supported"],
    [400, 'SECURITY_ERROR', 'ZIP_BOMB', 'expands to far more data than its file size'],
    [413, 'SECURITY_ERROR', 'OVERSIZED_FILE', 'larger than the upload limit'],
    [415, 'DATA_ERROR', 'UNSUPPORTED_FORMAT', 'Upload a .csv or .xlsx file'],
  ])('renders the %s %s(%s) reason as human text', async (status, code, reason, text) => {
    apiFor('analyst');
    FakeXhr.reply = envelope(status, code, `[${reason}] backend text`, { reason });
    renderApp('/datasets/upload');

    const user = await pickFile(new File(['a,b\n1,2\n'], 'data.xlsx'));
    await user.click(screen.getByRole('button', { name: 'Upload' }));

    await screen.findByRole('button', { name: 'Try again' });
    const alerts = screen.getAllByRole('alert');
    expect(alerts.some((a) => a.textContent?.includes(text))).toBe(true);
  });

  it('sends CSRF and Idempotency-Key headers and reuses the key on retry', async () => {
    apiFor('analyst');
    FakeXhr.reply = envelope(503, 'DATABASE_ERROR', 'temporarily unavailable');
    renderApp('/datasets/upload');

    const user = await pickFile(new File(['a,b\n1,2\n'], 'data.csv'));
    await user.click(screen.getByRole('button', { name: 'Upload' }));
    await screen.findByRole('button', { name: 'Try again' });
    const first = FakeXhr.last;
    expect(first?.method).toBe('POST');
    expect(first?.url).toBe('/api/v1/datasets');
    expect(first?.headers['X-CSRF-Token']).toBe('csrf-token-value');
    expect(first?.headers['Idempotency-Key']).toMatch(/^[0-9a-f]{32}$/);
    expect((first?.body?.get('file') as File).name).toBe('data.csv');

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(FakeXhr.last).not.toBe(first));
    expect(FakeXhr.last?.headers['Idempotency-Key']).toBe(first?.headers['Idempotency-Key']);
  });

  it('blocks files over the size limit before uploading', async () => {
    apiFor('analyst');
    renderApp('/datasets/upload');
    const big = new File(['x'], 'big.csv');
    Object.defineProperty(big, 'size', { value: MAX_UPLOAD_BYTES + 1 });

    await pickFile(big);

    expect(await screen.findByRole('alert')).toHaveTextContent('the upload limit is 200.0 MB');
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled();
    expect(FakeXhr.last).toBeNull();
  });

  it('shows the processing timeline and notifies when the profile is ready', async () => {
    let polls = 0;
    apiFor('analyst', {
      'GET /api/v1/datasets/ds_1/versions/dsv_1': () => {
        polls += 1;
        const state = polls < 2 ? 'INGESTING' : polls < 3 ? 'PROFILED' : 'READY';
        return json(versionBody(state, { row_count: 42 }));
      },
      'GET /api/v1/jobs/job_1': {
        body: { id: 'job_1', status: 'RUNNING', progress: 0, cancel_requested: false },
      },
    });
    FakeXhr.reply = { status: 201, body: UPLOADED };
    renderApp('/datasets/upload');

    const user = await pickFile(new File(['a,b\n1,2\n'], 'data.csv'));
    await user.click(screen.getByRole('button', { name: 'Upload' }));

    expect(
      await screen.findByRole('heading', { name: 'Processing your data' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('Reading and storing rows…')).toBeInTheDocument();
    // PROFILED is not the end: the understanding stages run next (backend jobs.py chain).
    expect(await screen.findByText('Understanding columns…')).toBeInTheDocument();
    expect(
      await screen.findByText('Processing finished.', { exact: false }, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.getByText('42 rows')).toBeInTheDocument();
    expect(await screen.findByText('Processing finished: data.csv')).toBeInTheDocument();
  });

  it('explains a processing failure with its reason code', async () => {
    apiFor('analyst', {
      'GET /api/v1/datasets/ds_1/versions/dsv_1': {
        body: versionBody('VALIDATION_FAILED', {
          failure: { code: 'MACRO_ENABLED', message: '[MACRO_ENABLED] macro' },
        }),
      },
      'GET /api/v1/jobs/job_1': {
        body: { id: 'job_1', status: 'FAILED', progress: 0, cancel_requested: false },
      },
    });
    FakeXhr.reply = { status: 201, body: UPLOADED };
    renderApp('/datasets/upload');

    const user = await pickFile(new File(['a'], 'data.xlsx'));
    await user.click(screen.getByRole('button', { name: 'Upload' }));

    expect(await screen.findByText('Reason code: MACRO_ENABLED')).toBeInTheDocument();
    expect(await screen.findByText('Processing failed: data.xlsx')).toBeInTheDocument();
  });

  it('does not offer the upload form to a viewer', async () => {
    apiFor('viewer');
    renderApp('/datasets/upload');

    expect(await screen.findByRole('alert')).toHaveTextContent('Requires Analyst role');
    expect(screen.queryByLabelText('Choose a file')).not.toBeInTheDocument();
  });
});
