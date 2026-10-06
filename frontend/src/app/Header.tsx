import { Link } from 'react-router';
import { Tooltip } from '../components/Tooltip';
import { useAuth } from '../context/AuthContext';
import { ROLE_LABELS } from '../context/permissions';
import { NotificationBell } from './NotificationBell';
import { useHealth } from '../api/useHealth';
import type { HealthState } from '../api/useHealth';

interface StatusDisplay {
  dotClass: string;
  label: string;
}

function statusDisplay(health: HealthState): StatusDisplay {
  switch (health.status) {
    case 'loading':
      return { dotClass: 'bg-text-muted', label: 'Checking API…' };
    case 'ok':
      return { dotClass: 'bg-success', label: `API ok · v${health.version}` };
    case 'error':
      return { dotClass: 'bg-warning', label: `API error · ${health.envelope.error_code}` };
    case 'unreachable':
      return { dotClass: 'bg-error', label: 'API unreachable' };
  }
}

export interface HeaderProps {
  assistantOpen: boolean;
  onToggleAssistant: () => void;
}

const quietButton =
  'text-caption rounded-sm border border-border px-3 py-1.5 font-medium text-text hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus';

/** Application header (UI-UX §10): workspace, privacy chip, API status, session, system banner. */
export function Header({ assistantOpen, onToggleAssistant }: HeaderProps) {
  const { session, logout } = useAuth();
  const health = useHealth();
  const status = statusDisplay(health);

  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface px-4">
        <div className="flex min-w-0 items-center gap-4">
          <span className="text-h3 text-text">Data Platform</span>
          {session ? (
            <span className="text-caption truncate text-text-muted">
              Workspace <span className="font-mono text-text">{session.workspace_id}</span>
            </span>
          ) : null}
        </div>

        <div className="flex items-center gap-4">
          <Tooltip label="Your data is processed by this self-hosted instance. No external AI services are used.">
            <Link
              to="/privacy"
              className="text-caption inline-flex items-center gap-1 rounded-sm px-2 py-1 text-text-muted hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              <span aria-hidden="true">{'🔒'}</span>
              Local processing
            </Link>
          </Tooltip>

          <span className="text-caption inline-flex items-center gap-1.5 text-text-muted">
            <span aria-hidden="true" className={`size-2 rounded-full ${status.dotClass}`} />
            {status.label}
          </span>

          <NotificationBell />

          <button
            type="button"
            aria-pressed={assistantOpen}
            onClick={onToggleAssistant}
            className={`${quietButton} aria-pressed:bg-primary aria-pressed:text-white`}
          >
            Ask AI
          </button>

          {session ? (
            <div className="flex items-center gap-2">
              <span className="text-caption text-right text-text-muted">
                <span className="block text-text">{session.email}</span>
                {ROLE_LABELS[session.role]}
              </span>
              <button type="button" onClick={() => void logout()} className={quietButton}>
                Sign out
              </button>
            </div>
          ) : null}
        </div>
      </header>
      {health.status === 'unreachable' ? (
        <div
          role="status"
          className="text-caption border-b border-error/40 bg-error/10 px-4 py-2 text-text"
        >
          The server can&apos;t be reached. Information on screen may be out of date; actions will
          fail until the connection returns.
        </div>
      ) : null}
    </>
  );
}
