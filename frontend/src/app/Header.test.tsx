import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider } from '../context/AuthContext';
import { NotificationProvider } from '../features/notifications/NotificationContext';
import { queryClient } from '../lib/queryClient';
import { HEALTH_OK, mockApi, session } from '../test/mockApi';
import { Header } from './Header';

function renderHeader() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AuthProvider>
          <NotificationProvider>
            <Header assistantOpen={false} onToggleAssistant={() => {}} />
          </NotificationProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Header backend status', () => {
  it('shows the API version once health resolves ok', async () => {
    mockApi({ 'GET /api/v1/health': HEALTH_OK, 'GET /api/v1/auth/me': { body: session() } });

    renderHeader();

    await waitFor(() => expect(screen.getByText('API ok · v0.1.0')).toBeInTheDocument());
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows unreachable and the system banner when the backend cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderHeader();

    await waitFor(() => expect(screen.getByText('API unreachable')).toBeInTheDocument());
    expect(screen.getByRole('status')).toHaveTextContent("The server can't be reached");
  });
});
