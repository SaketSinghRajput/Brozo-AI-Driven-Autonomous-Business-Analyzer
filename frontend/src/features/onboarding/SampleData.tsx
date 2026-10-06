import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '../../components/Button';

/** Entry in public/samples/index.json. Titles and descriptions are data, not code. */
interface Sample {
  file: string;
  title: string;
  description: string;
}

// Only plain file names inside /samples/ are fetched (no paths, no other origins).
const SAFE_FILE = /^[\w.-]+\.csv$/;

function isSample(value: unknown): value is Sample {
  const s = value as Sample;
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof s.file === 'string' &&
    SAFE_FILE.test(s.file) &&
    typeof s.title === 'string' &&
    typeof s.description === 'string'
  );
}

async function loadManifest(): Promise<Sample[]> {
  const response = await fetch('/samples/index.json');
  if (!response.ok) throw new Error(`samples manifest: HTTP ${response.status}`);
  const body = (await response.json()) as unknown;
  return Array.isArray(body) ? body.filter(isSample) : [];
}

/**
 * "Try with sample data" (UI-UX §12, TASK-UI-026): synthetic fixtures bundled with the app
 * (frontend/public/samples, copied from evals/fixtures). A picked sample goes through the normal
 * upload path, so it is processed exactly like a user file.
 */
export function SampleData({ onPick }: { onPick: (file: File) => void }) {
  const manifest = useQuery({ queryKey: ['samples'], queryFn: loadManifest, staleTime: Infinity });
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function pick(sample: Sample) {
    setLoading(sample.file);
    setError(null);
    try {
      const response = await fetch(`/samples/${sample.file}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      onPick(new File([blob], sample.file, { type: 'text/csv' }));
    } catch {
      setError(`The sample "${sample.title}" couldn't be loaded.`);
    } finally {
      setLoading(null);
    }
  }

  if (manifest.isError || (manifest.data && manifest.data.length === 0)) return null;
  return (
    <section id="samples" aria-labelledby="samples-heading" className="flex flex-col gap-3">
      <h2 id="samples-heading" className="text-h2 text-text">
        Try with sample data
      </h2>
      <p className="text-caption text-text-muted">
        Synthetic example files, generated for testing. They contain no real people or companies.
      </p>
      {manifest.isPending ? <p className="text-caption text-text-muted">Loading samples…</p> : null}
      <ul className="grid gap-3 md:grid-cols-3">
        {manifest.data?.map((sample) => (
          <li
            key={sample.file}
            className="flex flex-col gap-2 rounded-md border border-border bg-surface p-3"
          >
            <span className="text-body font-medium text-text">{sample.title}</span>
            <span className="text-caption flex-1 text-text-muted">{sample.description}</span>
            <Button
              variant="secondary"
              size="sm"
              loading={loading === sample.file}
              aria-label={`Use sample: ${sample.title}`}
              onClick={() => void pick(sample)}
            >
              Use this sample
            </Button>
          </li>
        ))}
      </ul>
      {error ? (
        <p role="alert" className="text-caption text-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}
