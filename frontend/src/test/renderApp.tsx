import { render } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { AuthProvider } from '../context/AuthContext';
import { queryClient } from '../lib/queryClient';
import { routes } from '../routes';

/** Renders the real route tree (auth guard, shell, pages) at `path`, like App.tsx does. */
export function renderApp(path: string) {
  queryClient.clear();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { ...view, router };
}
