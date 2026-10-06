import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { describeError } from '../../api/errors';
import type { ChatMessage, Conversation } from '../../api/generated/types.gen';
import { Button, EmptyState, ErrorState, Skeleton } from '../../components';
import type { Segment } from '../../contracts/generated/turn_event';
import { useMessages } from './api';
import { AnswerView } from './AnswerView';
import { StreamInterruptedError, streamTurn } from './stream';
import { EMPTY_TURN, applyEvent, isDegraded, type TurnView } from './turn';

const MAX_QUESTION = 2000; // backend MessageCreate.content max_length

type Phase = 'streaming' | 'done' | 'stopped' | 'interrupted' | 'failed';

interface LiveTurn {
  id: number;
  question: string;
  view: TurnView;
  phase: Phase;
  failure?: unknown;
}

/** UI-UX §38 degraded mode: the assistant is down, the deterministic analytics are not. */
export function DegradedBanner() {
  return (
    <p
      role="status"
      className="text-caption rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-text"
    >
      <span aria-hidden="true">⚠ </span>
      Assistant unavailable — dashboards still work. Only verified results are shown.
    </p>
  );
}

function Question({ text }: { text: string }) {
  return (
    <p className="text-body self-end rounded-md bg-primary/10 px-3 py-2 text-text">
      <span className="sr-only">You asked: </span>
      {text}
    </p>
  );
}

function StoredAnswer({ message }: { message: ChatMessage }) {
  // Stored number segments carry no value (only evidence id + field); they open the evidence.
  const segments = (message.segments ?? []).map((s) =>
    s.type === 'number' ? { ...s, value: null, unit: null } : s,
  ) as Segment[];
  return (
    <AnswerView
      view={{ ...EMPTY_TURN, segments }}
      state={message.state ?? null}
      fromHistory
      templated={message.answer_source === 'template'}
    />
  );
}

function TurnOutcome({
  turn,
  onAskAgain,
  onReload,
}: {
  turn: LiveTurn;
  onAskAgain: () => void;
  onReload: () => void;
}) {
  const { error } = turn.view;
  return (
    <>
      {error ? (
        <ErrorState
          message={error.message}
          retryable={error.retryable}
          onRetry={onAskAgain}
          traceId={error.trace_id ?? undefined}
        />
      ) : null}
      {turn.phase === 'stopped' ? (
        <p className="text-caption text-text-muted">Stopped. This answer was cancelled.</p>
      ) : null}
      {turn.phase === 'interrupted' ? (
        <div role="alert" className="flex flex-wrap items-center gap-2">
          <p className="text-caption text-text">
            The connection closed before the answer finished.
          </p>
          <Button size="sm" variant="secondary" onClick={onAskAgain}>
            Ask again
          </Button>
          <Button size="sm" variant="ghost" onClick={onReload}>
            Reload conversation
          </Button>
        </div>
      ) : null}
      {turn.phase === 'failed' ? (
        <ErrorState {...describeError(turn.failure)} onRetry={onAskAgain} />
      ) : null}
    </>
  );
}

/**
 * One conversation (UI-UX §24-25): stored history plus the turns asked in this session, streamed
 * over SSE. Stop aborts the request (the backend cancels the turn on disconnect).
 */
export function ChatThread({ conversation }: { conversation: Conversation }) {
  const messages = useMessages(conversation.id);
  const [turns, setTurns] = useState<LiveTurn[]>([]);
  const [draft, setDraft] = useState('');
  const controller = useRef<AbortController | null>(null);
  const nextId = useRef(0);
  const inputId = useId();
  const streaming = turns.some((t) => t.phase === 'streaming');
  const lastFinished = [...turns].reverse().find((t) => t.view.final || t.view.error);

  useEffect(() => () => controller.current?.abort(), []);

  function update(id: number, change: (turn: LiveTurn) => LiveTurn) {
    setTurns((all) => all.map((t) => (t.id === id ? change(t) : t)));
  }

  async function send(question: string) {
    const id = nextId.current++;
    const abort = new AbortController();
    controller.current = abort;
    setTurns((all) => [...all, { id, question, view: EMPTY_TURN, phase: 'streaming' }]);
    try {
      await streamTurn(conversation.id, question, {
        signal: abort.signal,
        onEvent: (event) => update(id, (t) => ({ ...t, view: applyEvent(t.view, event) })),
      });
      update(id, (t) => ({ ...t, phase: abort.signal.aborted ? 'stopped' : 'done' }));
    } catch (failure) {
      const phase = failure instanceof StreamInterruptedError ? 'interrupted' : 'failed';
      update(id, (t) => ({ ...t, phase, failure }));
    }
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const question = draft.trim();
    if (!question || streaming) return;
    setDraft('');
    void send(question);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function reload() {
    setTurns((all) => all.filter((t) => t.phase === 'streaming'));
    void messages.refetch();
  }

  const history = messages.data ?? [];
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <p className="text-caption text-text-muted">
        Asking about dataset version {conversation.dataset_version_id} · semantic model v
        {conversation.semantic_model_version}
      </p>
      {lastFinished && isDegraded(lastFinished.view) ? <DegradedBanner /> : null}

      <div aria-live="polite" className="flex flex-1 flex-col gap-4 overflow-y-auto">
        {messages.isPending ? <Skeleton variant="text" rows={4} /> : null}
        {messages.isError ? (
          <ErrorState {...describeError(messages.error)} onRetry={() => void messages.refetch()} />
        ) : null}
        {messages.isSuccess && history.length === 0 && turns.length === 0 ? (
          <EmptyState message="No questions yet. Ask one below." />
        ) : null}
        {history.map((message) =>
          message.role === 'user' ? (
            <Question key={message.id} text={message.content ?? ''} />
          ) : (
            <StoredAnswer key={message.id} message={message} />
          ),
        )}
        {turns.map((turn) => (
          <div key={turn.id} className="flex flex-col gap-2">
            <Question text={turn.question} />
            <AnswerView
              view={turn.view}
              state={turn.view.final?.state ?? null}
              streaming={turn.phase === 'streaming'}
              degraded={isDegraded(turn.view)}
            />
            <TurnOutcome
              turn={turn}
              onAskAgain={() => void send(turn.question)}
              onReload={reload}
            />
          </div>
        ))}
      </div>

      <form onSubmit={submit} className="flex flex-col gap-2 border-t border-border pt-3">
        <label htmlFor={inputId} className="text-caption font-medium text-text">
          Your question
        </label>
        <textarea
          id={inputId}
          value={draft}
          maxLength={MAX_QUESTION}
          rows={2}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about this data. Enter to send, Shift+Enter for a new line."
          className="text-body rounded-md border border-border bg-surface px-3 py-2 text-text focus-visible:outline-2 focus-visible:outline-focus"
        />
        <div className="flex justify-end gap-2">
          {streaming ? (
            <Button type="button" variant="secondary" onClick={() => controller.current?.abort()}>
              Stop
            </Button>
          ) : null}
          <Button type="submit" disabled={streaming || draft.trim() === ''}>
            Send
          </Button>
        </div>
      </form>
    </div>
  );
}
