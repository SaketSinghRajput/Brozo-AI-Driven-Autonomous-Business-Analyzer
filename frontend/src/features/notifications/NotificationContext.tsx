import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { Link } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { datasetKeys, FAILED_STATES, isProcessing, useVersion } from '../datasets/api';
import { rejectionText } from '../datasets/reasons';

/** UI-UX §52 categories. */
export type NoticeKind = 'success' | 'info' | 'warning' | 'error';

export interface Notice {
  id: number;
  kind: NoticeKind;
  title: string;
  body?: string;
  href?: string;
  createdAt: Date;
  read: boolean;
}

export type NewNotice = Pick<Notice, 'kind' | 'title' | 'body' | 'href'>;

/** An upload whose processing the user started and wants to hear about. */
export interface TrackedUpload {
  datasetId: string;
  versionId: string;
  label: string;
}

interface NotificationContextValue {
  notices: Notice[];
  unread: number;
  notify: (notice: NewNotice) => void;
  markAllRead: () => void;
  track: (upload: TrackedUpload) => void;
}

const NotificationContext = createContext<NotificationContextValue | null>(null);

// §52: success -> toast + centre; error -> toast until dismissed + centre; info/warning -> centre.
const TOASTED: ReadonlySet<NoticeKind> = new Set(['success', 'error']);
const TOAST_MS = 6000;
const MAX_NOTICES = 50;

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [notices, setNotices] = useState<Notice[]>([]);
  const [toastIds, setToastIds] = useState<number[]>([]);
  const [tracked, setTracked] = useState<TrackedUpload[]>([]);
  const nextId = useRef(1);

  const dismissToast = useCallback((id: number) => {
    setToastIds((ids) => ids.filter((other) => other !== id));
  }, []);

  const notify = useCallback(
    (notice: NewNotice) => {
      const id = nextId.current++;
      setNotices((all) =>
        [{ ...notice, id, createdAt: new Date(), read: false }, ...all].slice(0, MAX_NOTICES),
      );
      if (!TOASTED.has(notice.kind)) return;
      setToastIds((ids) => [...ids, id]);
      if (notice.kind === 'success') window.setTimeout(() => dismissToast(id), TOAST_MS);
    },
    [dismissToast],
  );

  const markAllRead = useCallback(() => {
    setNotices((all) => (all.some((n) => !n.read) ? all.map((n) => ({ ...n, read: true })) : all));
  }, []);

  const track = useCallback((upload: TrackedUpload) => {
    setTracked((all) =>
      all.some((t) => t.versionId === upload.versionId) ? all : [...all, upload],
    );
  }, []);

  const untrack = useCallback((versionId: string) => {
    setTracked((all) => all.filter((t) => t.versionId !== versionId));
  }, []);

  const value = useMemo<NotificationContextValue>(
    () => ({
      notices,
      unread: notices.filter((n) => !n.read).length,
      notify,
      markAllRead,
      track,
    }),
    [notices, notify, markAllRead, track],
  );

  const toasts = toastIds
    .map((id) => notices.find((n) => n.id === id))
    .filter((n): n is Notice => n !== undefined);

  return (
    <NotificationContext.Provider value={value}>
      {children}
      {tracked.map((upload) => (
        <UploadWatcher key={upload.versionId} upload={upload} notify={notify} onDone={untrack} />
      ))}
      <div
        aria-live="polite"
        className="pointer-events-none fixed right-4 bottom-4 z-40 flex w-80 flex-col gap-2"
      >
        {toasts.map((toast) => (
          <Toast key={toast.id} notice={toast} onDismiss={() => dismissToast(toast.id)} />
        ))}
      </div>
    </NotificationContext.Provider>
  );
}

export function useNotifications(): NotificationContextValue {
  const ctx = useContext(NotificationContext);
  if (!ctx) throw new Error('useNotifications must be used within a NotificationProvider');
  return ctx;
}

export const kindStyle: Record<NoticeKind, { icon: string; className: string }> = {
  success: { icon: '✓', className: 'border-l-success' },
  info: { icon: 'ⓘ', className: 'border-l-info' },
  warning: { icon: '⚑', className: 'border-l-warning' },
  error: { icon: '✕', className: 'border-l-error' },
};

function Toast({ notice, onDismiss }: { notice: Notice; onDismiss: () => void }) {
  const style = kindStyle[notice.kind];
  return (
    <div
      role={notice.kind === 'error' ? 'alert' : 'status'}
      className={`pointer-events-auto flex items-start gap-2 rounded-md border border-l-4 border-border bg-surface-raised p-3 shadow-md ${style.className}`}
    >
      <span aria-hidden="true" className="text-text">
        {style.icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-body font-medium text-text">{notice.title}</p>
        {notice.body ? <p className="text-caption text-text-muted">{notice.body}</p> : null}
        {notice.href ? (
          <Link
            to={notice.href}
            onClick={onDismiss}
            className="text-caption text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
          >
            Open
          </Link>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={`Dismiss: ${notice.title}`}
        className="text-caption size-6 rounded-sm text-text-muted hover:bg-surface focus-visible:outline-2 focus-visible:outline-focus"
      >
        ✕
      </button>
    </div>
  );
}

/**
 * Polls one uploaded version until the backend settles it, then raises exactly one notification
 * (UI-UX §52: fed by real state changes, never by timers).
 */
function UploadWatcher({
  upload,
  notify,
  onDone,
}: {
  upload: TrackedUpload;
  notify: (notice: NewNotice) => void;
  onDone: (versionId: string) => void;
}) {
  const queryClient = useQueryClient();
  const { data: version } = useVersion(upload.datasetId, upload.versionId);

  useEffect(() => {
    if (!version || isProcessing(version.state)) return;
    const base = `/datasets/${upload.datasetId}`;
    if (FAILED_STATES.has(version.state)) {
      notify({
        kind: 'error',
        title: `Processing failed: ${upload.label}`,
        body: rejectionText(version.failure?.code, version.failure?.message ?? 'Unknown error'),
        href: `${base}/overview?version=${version.id}`,
      });
    } else {
      notify({
        kind: 'success',
        title: `Processing finished: ${upload.label}`,
        href: `${base}/profile?version=${version.id}`,
      });
      const flags = version.review_flags?.length ?? 0;
      if (flags > 0) {
        notify({
          kind: 'info',
          title: `${flags} ${flags === 1 ? 'item needs' : 'items need'} your review: ${upload.label}`,
          href: `${base}/overview?version=${version.id}`,
        });
      }
    }
    void queryClient.invalidateQueries({ queryKey: datasetKeys.list });
    void queryClient.invalidateQueries({ queryKey: datasetKeys.versions(upload.datasetId) });
    onDone(upload.versionId);
  }, [version, upload, notify, onDone, queryClient]);

  return null;
}
