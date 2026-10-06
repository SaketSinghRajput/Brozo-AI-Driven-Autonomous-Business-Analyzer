import { Navigate, type RouteObject } from 'react-router';
import { AppShell } from './app/AppShell';
import { LoginPage } from './pages/LoginPage';
import { HomePage } from './pages/HomePage';
import { PrivacyPage } from './pages/PrivacyPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { DatasetsListPage } from './features/datasets/DatasetsListPage';
import { DatasetWorkspacePage } from './features/datasets/DatasetWorkspacePage';

/**
 * Route tree (UI-UX §9, design.md §55). AppShell guards everything except /login. Heavy pages
 * (Recharts, the generated contract validators, chat) are route-split: each loads its own
 * chunk on first visit, so the shell bundle stays small.
 */
export const routes: RouteObject[] = [
  { path: '/login', element: <LoginPage /> },
  {
    element: <AppShell />,
    children: [
      { path: '/', element: <HomePage /> },
      { path: '/datasets', element: <DatasetsListPage /> },
      {
        path: '/datasets/upload',
        lazy: () =>
          import('./features/datasets/UploadPage').then((m) => ({ Component: m.UploadPage })),
      },
      {
        path: '/datasets/:datasetId',
        element: <DatasetWorkspacePage />,
        children: [
          { index: true, element: <Navigate to="overview" replace /> },
          {
            path: 'overview',
            lazy: () =>
              import('./features/datasets/OverviewTab').then((m) => ({ Component: m.OverviewTab })),
          },
          {
            path: 'dashboard',
            lazy: () =>
              import('./features/dashboard/DashboardTab').then((m) => ({
                Component: m.DashboardTab,
              })),
          },
          {
            path: 'understanding',
            lazy: () =>
              import('./features/semantic/UnderstandingTab').then((m) => ({
                Component: m.UnderstandingTab,
              })),
          },
          {
            path: 'profile',
            lazy: () =>
              import('./features/datasets/ProfileTab').then((m) => ({ Component: m.ProfileTab })),
          },
          {
            path: 'history',
            lazy: () =>
              import('./features/datasets/HistoryTab').then((m) => ({ Component: m.HistoryTab })),
          },
        ],
      },
      {
        // Dev preview of fixture dashboards.
        path: '/dashboards/demo',
        lazy: () =>
          import('./features/dashboard/demo/DashboardDemoPage').then((m) => ({
            Component: m.DashboardDemoPage,
          })),
      },
      {
        path: '/assistant',
        lazy: () =>
          import('./features/chat/AssistantPage').then((m) => ({ Component: m.AssistantPage })),
      },
      {
        path: '/assistant/:conversationId',
        lazy: () =>
          import('./features/chat/AssistantPage').then((m) => ({ Component: m.AssistantPage })),
      },
      {
        path: '/history',
        lazy: () =>
          import('./pages/GlobalHistoryPage').then((m) => ({ Component: m.GlobalHistoryPage })),
      },
      {
        path: '/system',
        lazy: () =>
          import('./features/admin/SystemPage').then((m) => ({ Component: m.SystemPage })),
      },
      { path: '/privacy', element: <PrivacyPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
