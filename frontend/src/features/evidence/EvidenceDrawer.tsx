import {
  Suspense,
  createContext,
  lazy,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react';
import type { ValidationStatus } from '../../components';

export interface EvidenceRequest {
  evidenceId: string;
  /** What is being explained, e.g. the widget title and displayed value. */
  claim: string;
  status: ValidationStatus;
}

type OpenEvidence = (request: EvidenceRequest) => void;

const EvidenceContext = createContext<OpenEvidence | null>(null);

// The dialog body (and the evidence contract validator it uses) loads on first open.
const EvidenceDrawerPanel = lazy(() =>
  import('./EvidenceDrawerPanel').then((module) => ({ default: module.EvidenceDrawerPanel })),
);

/** Opens the evidence drawer. Every displayed number calls this in one click (PRD-UX-004). */
export function useOpenEvidence(): OpenEvidence {
  const open = useContext(EvidenceContext);
  if (!open) throw new Error('EvidenceDrawerProvider is missing');
  return open;
}

export function EvidenceDrawerProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<EvidenceRequest | null>(null);
  const open = useCallback<OpenEvidence>((next) => setRequest(next), []);
  return (
    <EvidenceContext.Provider value={open}>
      {children}
      {request ? (
        <Suspense fallback={null}>
          <EvidenceDrawerPanel
            key={request.evidenceId}
            request={request}
            onClose={() => setRequest(null)}
          />
        </Suspense>
      ) : null}
    </EvidenceContext.Provider>
  );
}

/** A button that opens evidence; used next to every displayed number. */
export function EvidenceLink({
  request,
  label = 'Evidence →',
}: {
  request: EvidenceRequest;
  label?: string;
}) {
  const open = useOpenEvidence();
  return (
    <button
      type="button"
      onClick={() => open(request)}
      aria-label={`Evidence for ${request.claim}`}
      className="text-caption rounded-sm font-medium text-primary underline-offset-2 hover:underline"
    >
      {label}
    </button>
  );
}
