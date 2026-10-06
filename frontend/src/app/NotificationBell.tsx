import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router';
import { kindStyle, useNotifications } from '../features/notifications/NotificationContext';

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });

/** Header notification centre (UI-UX §52): keeps important events after their toast is gone. */
export function NotificationBell() {
  const { notices, unread, markAllRead } = useNotifications();
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    markAllRead();
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, markAllRead]);

  const label = unread > 0 ? `Notifications, ${unread} unread` : 'Notifications';

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={label}
        onClick={() => setOpen((value) => !value)}
        className="text-caption relative rounded-sm border border-border px-2 py-1.5 text-text hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        <span aria-hidden="true">🔔</span>
        {unread > 0 ? (
          <span
            aria-hidden="true"
            className="text-label absolute -top-1.5 -right-1.5 rounded-full bg-error px-1.5 text-white"
          >
            {unread}
          </span>
        ) : null}
      </button>
      {open ? (
        <section
          id={panelId}
          aria-label="Notification centre"
          className="absolute right-0 z-30 mt-2 max-h-96 w-80 overflow-y-auto rounded-md border border-border bg-surface-raised p-2 shadow-lg"
        >
          {notices.length === 0 ? (
            <p className="text-caption p-2 text-text-muted">No notifications yet.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {notices.map((notice) => (
                <li
                  key={notice.id}
                  className={`rounded-sm border-l-4 p-2 ${kindStyle[notice.kind].className}`}
                >
                  <p className="text-caption font-medium text-text">
                    <span aria-hidden="true">{kindStyle[notice.kind].icon} </span>
                    {notice.title}
                  </p>
                  {notice.body ? (
                    <p className="text-caption text-text-muted">{notice.body}</p>
                  ) : null}
                  <p className="text-caption text-text-muted">
                    {timeFormat.format(notice.createdAt)}
                    {notice.href ? (
                      <>
                        {' · '}
                        <Link
                          to={notice.href}
                          onClick={() => setOpen(false)}
                          className="text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
                        >
                          Open
                        </Link>
                      </>
                    ) : null}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </div>
  );
}
