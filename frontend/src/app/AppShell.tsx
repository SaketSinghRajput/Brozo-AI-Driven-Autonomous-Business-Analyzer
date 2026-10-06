import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router';
import { Header } from './Header';
import { Sidebar } from './Sidebar';
import { Skeleton } from '../components/Surfaces';

// The docked assistant (chat, answer rendering) loads when it is first opened.
const AssistantPanel = lazy(() =>
  import('../features/chat/AssistantPanel').then((module) => ({
    default: module.AssistantPanel,
  })),
);
import { useAuth } from '../context/AuthContext';
import { NotificationProvider } from '../features/notifications/NotificationContext';
import { DashboardDataProvider } from '../features/dashboard/dataSource';
import { apiDataSource } from '../features/evidence/apiSource';
import { EvidenceDrawerProvider } from '../features/evidence/EvidenceDrawer';

/**
 * Top-level layout: header, sidebar, main content, docked assistant panel (UI-UX §10).
 * Also the auth guard: no session -> /login?next=<current path> (TASK-UI-001).
 */
export function AppShell() {
  const { status } = useAuth();
  const location = useLocation();
  const [assistantOpen, setAssistantOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const firstPath = useRef<string | null>(location.pathname);

  // UI-UX §53: after in-app navigation, move focus to the new page so keyboard and screen-reader
  // users start at its content instead of the link they left. Not on first load; search-only
  // changes (e.g. ?version=) keep focus where it is.
  useEffect(() => {
    if (location.pathname === firstPath.current) return;
    firstPath.current = null;
    mainRef.current?.focus();
  }, [location.pathname]);

  if (status === 'loading') {
    return (
      <div className="p-6" role="status" aria-label="Checking your session">
        <Skeleton variant="card" />
      </div>
    );
  }
  if (status === 'anonymous') {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }

  return (
    <NotificationProvider>
      {/* Evidence drawer for every screen that shows backend results (chat, semantic). */}
      <DashboardDataProvider source={apiDataSource}>
        <EvidenceDrawerProvider>
          <div className="flex h-screen flex-col">
            <a
              href="#main-content"
              className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-white"
            >
              Skip to main content
            </a>
            <Header
              assistantOpen={assistantOpen}
              onToggleAssistant={() => setAssistantOpen((v) => !v)}
            />
            <div className="flex flex-1 overflow-hidden">
              <Sidebar />
              <main
                ref={mainRef}
                id="main-content"
                tabIndex={-1}
                className="flex-1 overflow-y-auto p-6 focus:outline-none"
              >
                <Outlet />
              </main>
              {assistantOpen ? (
                <Suspense fallback={<Skeleton variant="card" className="w-80" />}>
                  <AssistantPanel />
                </Suspense>
              ) : null}
            </div>
          </div>
        </EvidenceDrawerProvider>
      </DashboardDataProvider>
    </NotificationProvider>
  );
}
