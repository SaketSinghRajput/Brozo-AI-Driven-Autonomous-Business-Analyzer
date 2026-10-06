import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router';
import { Button } from '../components/Button';
import { useAuth } from '../context/AuthContext';
import { ApiError, describeError } from '../api/errors';

/** Only same-app paths are allowed as a post-login destination (no open redirect). */
export function safeReturnPath(next: string | null): string {
  return next && next.startsWith('/') && !next.startsWith('//') ? next : '/';
}

/** "about 2 minutes" / "about 40 seconds"; rounded up so the user never retries too early. */
export function waitText(seconds: number): string {
  if (seconds >= 90) return `about ${Math.ceil(seconds / 60)} minutes`;
  if (seconds > 1) return `about ${seconds} seconds`;
  return 'a moment';
}

export function loginErrorText(error: unknown): string {
  // The backend gives the same 401 for an unknown email and a wrong password, and throttles
  // (429) whether or not the email exists; the text must not hint either way.
  if (error instanceof ApiError && error.status === 401) return 'Email or password is incorrect.';
  if (error instanceof ApiError && error.status === 429) {
    const wait = error.retryAfterSeconds;
    return `Too many sign-in attempts. Try again in ${wait === null ? 'a few minutes' : waitText(wait)}.`;
  }
  return describeError(error).message;
}

/** Sign-in (TASK-UI-001, ADR-0013). The session cookie is set by the backend; JS never sees it. */
export function LoginPage() {
  const { status, expired, login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeReturnPath(params.get('next'));
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (status === 'authenticated' && !submitting) return <Navigate to={next} replace />;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email, password);
      void navigate(next, { replace: true });
    } catch (caught) {
      setError(loginErrorText(caught));
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-6">
      <form
        onSubmit={(event) => void handleSubmit(event)}
        aria-describedby={error ? 'login-error' : undefined}
        className="flex w-full max-w-sm flex-col gap-4 rounded-md border border-border bg-surface p-6"
      >
        <h1 className="text-h1 text-text">Sign in</h1>
        {expired ? (
          <p role="status" className="text-caption rounded-sm bg-info/10 p-2 text-text">
            Your session has expired. Sign in again to continue.
          </p>
        ) : null}
        <label className="flex flex-col gap-1">
          <span className="text-label text-text">Email</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="text-body rounded-sm border border-border bg-surface px-3 py-2 text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-label text-text">Password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="text-body rounded-sm border border-border bg-surface px-3 py-2 text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          />
        </label>
        {error ? (
          <p id="login-error" role="alert" className="text-caption text-error">
            {error}
          </p>
        ) : null}
        <Button type="submit" loading={submitting}>
          Sign in
        </Button>
        <p className="text-caption text-text-muted">
          Your data is processed by this self-hosted instance. No external AI services are used.
        </p>
      </form>
    </main>
  );
}
