import { describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HEALTH_OK, envelope, json, mockApi, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { safeReturnPath, waitText } from '../pages/LoginPage';

describe('auth guard and login (TASK-UI-001)', () => {
  it('redirects to login preserving the return path when there is no session', async () => {
    mockApi({
      'GET /api/v1/auth/me': envelope(401, 'AUTH_ERROR', 'not authenticated'),
      'GET /api/v1/health': HEALTH_OK,
    });
    const { router } = renderApp('/history?tab=2');

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/history?tab=2')}`);
    expect(screen.queryByText(/session has expired/i)).not.toBeInTheDocument();
  });

  it('signs in, returns to the saved path, and sends the CSRF header on sign-out', async () => {
    let loggedIn = false;
    const { calls } = mockApi({
      'GET /api/v1/auth/me': () =>
        loggedIn ? json(session()) : json(envelope(401, 'AUTH_ERROR', 'x').body, 401),
      'POST /api/v1/auth/login': () => {
        loggedIn = true;
        return json(session());
      },
      'POST /api/v1/auth/logout': { status: 204 },
      'GET /api/v1/health': HEALTH_OK,
    });
    const user = userEvent.setup();
    const { router } = renderApp('/history');

    await user.type(await screen.findByLabelText('Email'), 'person@example.test');
    await user.type(screen.getByLabelText('Password'), 'correct horse');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/history'));
    const loginCall = calls.find((c) => c.url.endsWith('/auth/login'));
    expect(await loginCall?.clone().json()).toEqual({
      email: 'person@example.test',
      password: 'correct horse',
    });
    expect(await screen.findByText('person@example.test')).toBeInTheDocument();
    expect(screen.getByText('ws_abc123')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    const logoutCall = calls.find((c) => c.url.endsWith('/auth/logout'));
    expect(logoutCall?.headers.get('X-CSRF-Token')).toBe('csrf-token-value');
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'));
  });

  it('shows a neutral message for wrong credentials', async () => {
    mockApi({
      'GET /api/v1/auth/me': envelope(401, 'AUTH_ERROR', 'x'),
      'POST /api/v1/auth/login': envelope(401, 'AUTH_ERROR', 'invalid credentials'),
      'GET /api/v1/health': HEALTH_OK,
    });
    const user = userEvent.setup();
    renderApp('/');

    await user.type(await screen.findByLabelText('Email'), 'a@b.test');
    await user.type(screen.getByLabelText('Password'), 'nope');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Email or password is incorrect.');
  });

  it('shows the throttle wait time from Retry-After without hinting whether the account exists', async () => {
    mockApi({
      'GET /api/v1/auth/me': envelope(401, 'AUTH_ERROR', 'x'),
      'POST /api/v1/auth/login': () => {
        const body = envelope(429, 'RATE_LIMITED', 'too many failed login attempts').body;
        return new Response(JSON.stringify(body), {
          status: 429,
          headers: { 'Content-Type': 'application/json', 'Retry-After': '150' },
        });
      },
      'GET /api/v1/health': HEALTH_OK,
    });
    const user = userEvent.setup();
    renderApp('/');

    await user.type(await screen.findByLabelText('Email'), 'a@b.test');
    await user.type(screen.getByLabelText('Password'), 'nope');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Too many sign-in attempts. Try again in about 3 minutes.');
    expect(alert.textContent).not.toMatch(/account|exist|incorrect/i);
  });

  it('formats throttle waits', () => {
    expect(waitText(40)).toBe('about 40 seconds');
    expect(waitText(90)).toBe('about 2 minutes');
    expect(waitText(0)).toBe('a moment');
  });

  it('only accepts same-app return paths', () => {
    expect(safeReturnPath('/datasets/ds_1')).toBe('/datasets/ds_1');
    expect(safeReturnPath('//elsewhere.test/x')).toBe('/');
    expect(safeReturnPath('https://elsewhere.test')).toBe('/');
    expect(safeReturnPath(null)).toBe('/');
  });
});

describe('permission-aware shell (TASK-UI-002)', () => {
  it('hides System from a viewer and explains on direct access', async () => {
    mockApi({
      'GET /api/v1/auth/me': { body: session('viewer') },
      'GET /api/v1/health': HEALTH_OK,
    });
    renderApp('/system');

    expect(
      await screen.findByText('This area requires administrator permission.'),
    ).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    expect(within(nav).queryByRole('link', { name: 'System' })).not.toBeInTheDocument();
    expect(screen.getByText('Viewer')).toBeInTheDocument();
  });

  it('shows System to an administrator', async () => {
    mockApi({ 'GET /api/v1/auth/me': { body: session('admin') }, 'GET /api/v1/health': HEALTH_OK });
    renderApp('/');

    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    expect(within(nav).getByRole('link', { name: 'System' })).toBeInTheDocument();
  });
});
