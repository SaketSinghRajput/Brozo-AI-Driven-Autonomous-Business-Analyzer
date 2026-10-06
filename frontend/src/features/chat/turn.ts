import type {
  FinalEvent,
  Segment,
  ToolCallEvent,
  TurnError,
  TurnEvent,
  TurnState,
  ValidationEvent,
} from '../../contracts/generated/turn_event';

/** Everything the UI knows about one assistant turn, folded from its TurnEvents. */
export interface TurnView {
  stages: TurnState[];
  tools: ToolCallEvent[];
  validation: ValidationEvent | null;
  segments: Segment[];
  final: FinalEvent | null;
  error: TurnError | null;
}

export const EMPTY_TURN: TurnView = {
  stages: [],
  tools: [],
  validation: null,
  segments: [],
  final: null,
  error: null,
};

/** Pure reducer (backend/app/orchestrator/domain.py documents the event order). */
export function applyEvent(view: TurnView, { event }: TurnEvent): TurnView {
  switch (event.type) {
    case 'status':
      return { ...view, stages: [...view.stages, event.stage] };
    case 'tool_call':
      return { ...view, tools: [...view.tools, event] };
    case 'validation':
      return { ...view, validation: event };
    case 'segment': {
      const segments = [...view.segments];
      segments[event.index] = event.segment;
      return { ...view, segments };
    }
    case 'error':
      return { ...view, error: event.error };
    case 'final':
      return { ...view, final: event };
    default:
      return view;
  }
}

/** Validation status of the answer: the validation event, else what `final` reports. */
export function validationStatus(view: TurnView) {
  return view.validation?.status ?? view.final?.validation_status ?? null;
}

/** UI-UX §38: degraded mode = the assistant (LLM) is unavailable; analytics still work. */
export function isDegraded(view: TurnView): boolean {
  return view.final?.mode === 'degraded' || view.error?.error_code === 'LLM_ERROR';
}
