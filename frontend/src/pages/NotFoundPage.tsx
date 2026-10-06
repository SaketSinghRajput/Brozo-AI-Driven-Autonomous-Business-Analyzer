import { Link } from 'react-router';

/** Fallback route for unknown paths. */
export function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-center">
      <h1 className="text-h1 text-text">Page not found</h1>
      <p className="text-body text-text-muted">The page you are looking for does not exist.</p>
      <Link to="/" className="text-body text-primary underline">
        Back to home
      </Link>
    </div>
  );
}
