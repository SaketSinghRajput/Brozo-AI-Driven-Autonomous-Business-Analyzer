import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FetchEventSourceInit } from '@microsoft/fetch-event-source';
import { HEALTH_OK, mockApi, session } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';
import { evidence } from '../dashboard/fixtures/build';
import { EvidenceDrawerProvider } from '../evidence/EvidenceDrawer';
import { TEMPLATED_NOTE } from './AnswerView';
import { CAUSAL_CERTAINTY, RootCausePanel } from './RootCausePanel';

/**
 * fetch-event-source is replaced by a scripted stream: each test sets the events the "server"
 * sends; `hold` keeps the stream open until the request is aborted.
 */
const sse = vi.hoisted(() => ({
  script: [] as object[],
  hold: false,
  calls: [] as { url: string; init: FetchEventSourceInit }[],
}));

vi.mock('@microsoft/fetch-event-source', () => ({
  fetchEventSource: vi.fn(async (url: string, init: FetchEventSourceInit) => {
    sse.calls.push({ url, init });
    await init.onopen?.(
      new Response('', { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    );
    sse.script.forEach((event, seq) => {
      const data = JSON.stringify({ schema_version: '1.0', seq, event });
      init.onmessage?.({ id: String(seq), event: '', data });
    });
    if (sse.hold) {
      await new Promise((resolve) => init.signal?.addEventListener('abort', resolve));
    }
  }),
}));

const CONVERSATION = {
  id: 'cv_1',
  owner_id: 'usr_1',
  dataset_version_id: 'dsv_1',
  semantic_model_version: 1,
  title: 'Questions about Export',
  created_at: '2026-09-28T10:00:00Z',
};

const USAGE = { steps: 8, tool_calls: 1, llm_tokens: 1200, retries: 0 };
const CONFIDENCE = { level: 'high', score: 0.9, basis: ['Deterministic calculation'] };
const TOOL = {
  type: 'tool_call',
  name: 'analytics.metric',
  args_summary: 'metric, time',
  status: 'ok',
  evidence_id: 'ev_1',
};
const NUMBER = {
  type: 'number',
  evidence_id: 'ev_1',
  field: 'result.value',
  format: 'pct:1',
  value: 0.125,
  unit: 'pct',
};
const STORED_NUMBER = {
  type: 'number',
  evidence_id: 'ev_1',
  field: 'result.value',
  format: 'pct:1',
};

function validation(status: string, issues: object[] = []) {
  return { type: 'validation', validation_id: 'va_1', status, issues, confidence: CONFIDENCE };
}

function final(state: string, extra: object = {}) {
  return { type: 'final', message_id: 'msg_2', state, usage: USAGE, ...extra };
}

function answered(status: string, segments: object[], state = 'COMPLETED', issues: object[] = []) {
  return [
    { type: 'status', stage: 'CLASSIFYING' },
    { type: 'status', stage: 'PLANNING' },
    { type: 'status', stage: 'EXECUTING' },
    TOOL,
    { type: 'status', stage: 'COMPOSING' },
    { type: 'status', stage: 'VALIDATING' },
    validation(status, issues),
    ...segments.map((segment, index) => ({ type: 'segment', index, segment })),
    final(state, { validation_status: status }),
  ];
}

function turnError(code: string, message: string) {
  return { type: 'error', error: { error_code: code, message, retryable: true, trace_id: null } };
}

function renderChat(messages: object[] = []) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session('analyst') },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/chat/conversations': { body: [CONVERSATION] },
    'GET /api/v1/chat/conversations/cv_1/messages': { body: messages },
    'GET /api/v1/evidence/ev_1': {
      body: evidence('ev_1', 'dsv_1', {
        value: 0.125,
        unit: 'pct',
        operation: 'metric',
        calculation: 'percent change of the measure',
      }),
    },
  });
}

async function ask(question = 'How did it change?') {
  const user = userEvent.setup();
  renderApp('/assistant/cv_1');
  await user.type(await screen.findByLabelText('Your question'), question);
  await user.click(screen.getByRole('button', { name: 'Send' }));
  return user;
}

beforeEach(() => {
  sse.script = [];
  sse.hold = false;
  sse.calls = [];
});

afterEach(() => vi.unstubAllGlobals());

describe('AI chat', () => {
  it('streams a validated answer whose number opens the evidence drawer in one click', async () => {
    renderChat();
    sse.script = answered('VALID', [
      { type: 'text', text: 'The measure changed by ' },
      NUMBER,
      { type: 'text', text: ' over the period.' },
      { type: 'caveat', text: 'Rows with missing dates are excluded.' },
    ]);
    const user = await ask();

    // The stream starts asynchronously after Send; wait for its output before reading the call.
    expect(await screen.findByText('Validated')).toBeInTheDocument();
    expect(sse.calls[0].url).toMatch(/\/api\/v1\/chat\/conversations\/cv_1\/messages$/);
    expect(sse.calls[0].init.body).toBe(JSON.stringify({ content: 'How did it change?' }));
    expect(screen.getByText('Checking results')).toBeInTheDocument();
    expect(screen.getByText('Calculated')).toBeInTheDocument();
    expect(screen.getByText('AI interpretation')).toBeInTheDocument();
    expect(screen.getByText(/How this was produced/)).toBeInTheDocument();
    expect(screen.getByText('Rows with missing dates are excluded.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Evidence for 12.5%' }));
    const drawer = await screen.findByRole('dialog');
    expect(await within(drawer).findByText('percent change of the measure')).toBeInTheDocument();
  });

  it('withholds numbers when the answer requires review', async () => {
    renderChat();
    sse.script = answered(
      'REQUIRES_REVIEW',
      [{ type: 'text', text: 'Changed by ' }, NUMBER],
      'REQUIRES_REVIEW',
    );
    await ask();

    expect(await screen.findByText('Needs review')).toBeInTheDocument();
    expect(screen.getByText(/Held for review/)).toBeInTheDocument();
    expect(screen.queryByText('12.5%')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Evidence for 12.5/ })).not.toBeInTheDocument();
  });

  it('says when the answer was written from the analysis results', async () => {
    renderChat();
    const script = answered('VALID', [{ type: 'text', text: 'Changed by ' }, NUMBER]);
    script[script.length - 1] = final('COMPLETED', {
      validation_status: 'VALID',
      answer_source: 'template',
    });
    sse.script = script;
    await ask();

    expect(await screen.findByText(TEMPLATED_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Evidence for 12.5%' })).toBeInTheDocument();
  });

  it('shows no provenance line for the assistant’s own answer', async () => {
    renderChat();
    sse.script = answered('VALID', [{ type: 'text', text: 'Changed by ' }, NUMBER]);
    await ask();

    expect(await screen.findByText('Validated')).toBeInTheDocument();
    expect(screen.queryByText(TEMPLATED_NOTE)).not.toBeInTheDocument();
  });

  it('marks an uncertain answer as Unverified with the reason', async () => {
    renderChat();
    sse.script = answered('UNCERTAIN', [NUMBER], 'COMPLETED', [
      { code: 'SOFT', severity: 'soft', target: 's0', detail: 'Wording not confirmed.' },
    ]);
    await ask();

    expect(await screen.findByText('Unverified')).toBeInTheDocument();
    expect(screen.getByText('Unverified: Wording not confirmed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Evidence for 12.5%' })).toBeInTheDocument();
  });

  it('shows the degraded banner when the assistant is unavailable', async () => {
    renderChat();
    sse.script = [
      { type: 'status', stage: 'CLASSIFYING' },
      turnError('LLM_ERROR', 'The assistant is unavailable right now.'),
      { type: 'segment', index: 0, segment: { type: 'caveat', text: 'Only verified results.' } },
      final('RECOVERED', { mode: 'degraded' }),
    ];
    await ask();

    expect(
      await screen.findByText(/Assistant unavailable — dashboards still work/),
    ).toBeInTheDocument();
  });

  it('explains a refusal as insufficient data with what to do next', async () => {
    renderChat();
    sse.script = [
      { type: 'status', stage: 'CLASSIFYING' },
      {
        type: 'segment',
        index: 0,
        segment: { type: 'text', text: 'Insufficient data: no time column.' },
      },
      final('REFUSED'),
    ];
    await ask();

    expect(
      await screen.findByText("I can't reliably answer this from the available data."),
    ).toBeInTheDocument();
    expect(screen.getByText('Insufficient data: no time column.')).toBeInTheDocument();
    expect(screen.getByText(/What you can do/)).toBeInTheDocument();
  });

  it('asks for clarification instead of answering', async () => {
    renderChat();
    sse.script = [
      { type: 'segment', index: 0, segment: { type: 'text', text: 'Which period do you mean?' } },
      final('NEEDS_CLARIFICATION'),
    ];
    await ask();

    expect(await screen.findByText('The assistant needs one more detail')).toBeInTheDocument();
    expect(screen.getByText('Which period do you mean?')).toBeInTheDocument();
  });

  it('stops a running turn by aborting the request', async () => {
    renderChat();
    sse.hold = true;
    sse.script = [{ type: 'status', stage: 'CLASSIFYING' }];
    const user = await ask();

    expect(await screen.findByText('Understanding your question')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(await screen.findByText('Stopped. This answer was cancelled.')).toBeInTheDocument();
    expect(sse.calls[0].init.signal?.aborted).toBe(true);
  });

  it('offers a retry for a retryable turn error', async () => {
    renderChat();
    sse.script = [turnError('SYSTEM_ERROR', 'The answer failed unexpectedly.'), final('RECOVERED')];
    const user = await ask();

    await user.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(sse.calls).toHaveLength(2);
    expect(sse.calls[1].init.body).toBe(JSON.stringify({ content: 'How did it change?' }));
  });

  it('keeps a space between words and numbers the model wrote without one', async () => {
    // Live qwen2.5 output (E2E-DEMO): "fell by" + number + " in August" rendered "fell by-6,978.90".
    renderChat();
    sse.script = answered('VALID', [
      { type: 'text', text: 'The measure fell by' },
      NUMBER,
      { type: 'text', text: ' over the period.' },
    ]);
    await ask();
    const number = await screen.findByRole('button', { name: 'Evidence for 12.5%' });
    expect(number.parentElement?.textContent).toBe('The measure fell by 12.5% over the period.');
  });

  it('shows the size of a change after a direction word, not a double negative', async () => {
    // B-5 (live demo): "fell by −6,978.90 … a −43.4% decrease". The backend marks numbers whose
    // sign a direction word states (`absolute`); the value stays signed for the evidence drawer.
    renderChat();
    sse.script = answered('VALID', [
      { type: 'text', text: 'The measure fell by ' },
      { ...NUMBER, value: -0.434, absolute: true },
      { type: 'text', text: '; it changed by ' },
      { ...NUMBER, value: -0.434 },
      { type: 'text', text: '.' },
    ]);
    await ask();
    const fell = await screen.findByRole('button', { name: 'Evidence for 43.4%' });
    expect(fell.parentElement?.textContent).toBe(
      'The measure fell by 43.4%; it changed by -43.4%.',
    );
  });

  it('shows the streamed root-cause factors in the contributing factors panel', async () => {
    renderChat();
    const explain = {
      ...TOOL,
      name: 'analytics.explain_change',
      findings: [
        {
          factor: 'Channel=Online',
          change: -3047.54,
          unit: 'currency',
          share: 43.7,
          relation_type: 'contribution',
        },
        {
          factor: 'Channel=Store',
          change: -2619.61,
          unit: 'currency',
          share: 37.5,
          relation_type: 'contribution',
        },
      ],
    };
    sse.script = answered('VALID', [{ type: 'text', text: 'Revenue fell.' }]).map((event) =>
      event === TOOL ? explain : event,
    );
    await ask('Why did it drop?');
    expect(await screen.findByText('Contributing factors')).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'Channel=Online' })).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'Channel=Store' })).toBeInTheDocument();
    expect(screen.getAllByText('Contribution')).toHaveLength(2);
  });

  it('reopens stored history and still withholds held answers', async () => {
    const at = '2026-09-28T10:00:00Z';
    renderChat([
      {
        id: 'm1',
        conversation_id: 'cv_1',
        role: 'user',
        content: 'First question',
        created_at: at,
      },
      {
        id: 'm2',
        conversation_id: 'cv_1',
        role: 'assistant',
        state: 'COMPLETED',
        segments: [{ type: 'text', text: 'It was ' }, STORED_NUMBER],
        created_at: at,
      },
      {
        id: 'm3',
        conversation_id: 'cv_1',
        role: 'assistant',
        state: 'REQUIRES_REVIEW',
        segments: [STORED_NUMBER],
        created_at: at,
      },
    ]);
    renderApp('/assistant/cv_1');

    expect(await screen.findByText('First question')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Evidence for this value' })).toHaveLength(1);
    expect(screen.getByText(/Held for review/)).toBeInTheDocument();
  });

  it('lists conversations in history and reopens one in the assistant', async () => {
    const { calls } = renderChat();
    const user = userEvent.setup();
    const { router } = renderApp('/history');

    await user.click(await screen.findByRole('link', { name: 'Questions about Export' }));
    expect(router.state.location.pathname).toBe('/assistant/cv_1');
    expect(await screen.findByText('No questions yet. Ask one below.')).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith('/chat/conversations/cv_1/messages'))).toBe(true);
  });
});

describe('RootCausePanel', () => {
  it('labels drivers as contributions and never claims a cause', () => {
    const { container } = render(
      <EvidenceDrawerProvider>
        <RootCausePanel
          evidence={{ evidenceId: 'ev_1', claim: 'Explain change', status: 'VALID' }}
          findings={[
            {
              factor: 'Group=A',
              relation_type: 'contribution',
              magnitude: -40,
              unit: null,
              share_pct: 0.8,
            },
            {
              factor: 'Group=B',
              relation_type: 'contribution',
              magnitude: -10,
              unit: null,
              share_pct: 0.2,
            },
          ]}
        />
      </EvidenceDrawerProvider>,
    );
    expect(screen.getAllByText('Contribution')).toHaveLength(2);
    // share_pct is a fraction (backend root_cause.py): 0.8 is shown as 80.0% of the change
    expect(container.textContent).toContain('(80.0% of change)');
    expect(screen.getByText(CAUSAL_CERTAINTY)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\bcause[sd]?\b/i);
  });
});
