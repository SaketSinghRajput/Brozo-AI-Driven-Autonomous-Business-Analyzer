import { useId, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react';
import { Link } from 'react-router';
import { ApiError, describeError } from '../../api/errors';
import type { UploadResponse } from '../../api/generated/types.gen';
import { Button } from '../../components/Button';
import { ErrorState } from '../../components/StateViews';
import { useNotifications } from '../notifications/NotificationContext';
import { uploadDataset } from './api';
import { ProcessingTimeline } from './ProcessingTimeline';
import { rejectionText } from './reasons';

/**
 * Mirror of the backend default `storage.max_upload_bytes` (backend/app/core/config.py, 200 MiB).
 * The limit is not exposed by the API yet, so this is a courtesy precheck only; the backend
 * enforces its configured limit and answers OVERSIZED_FILE (413) if it differs.
 */
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;
const ACCEPT = '.csv,.xlsx';

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Hex key from getRandomValues (randomUUID needs a secure context; LAN http may not be one). */
function newIdempotencyKey(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, '0'),
  ).join('');
}

type Phase =
  | { kind: 'idle' }
  | { kind: 'uploading'; loaded: number; total: number }
  | { kind: 'failed'; error: unknown }
  | { kind: 'processing'; result: UploadResponse; created: boolean };

interface Selection {
  file: File;
  /** One key per chosen file: "Try again" replays safely instead of creating a duplicate. */
  key: string;
}

function uploadErrorDisplay(error: unknown) {
  const display = describeError(error);
  if (error instanceof ApiError && error.status !== 401 && error.status !== 403) {
    return { ...display, message: rejectionText(error.reason, display.message) };
  }
  return display;
}

export interface DatasetUploaderProps {
  /** Pre-selected file (sample data). */
  initialFile?: File;
}

/** Dataset upload (UI-UX §13): drop zone, size precheck, byte progress, processing timeline. */
export function DatasetUploader({ initialFile }: DatasetUploaderProps) {
  const { track, notify } = useNotifications();
  const inputId = useId();
  const nameId = useId();
  const abortRef = useRef<AbortController | null>(null);
  const [selection, setSelection] = useState<Selection | null>(
    initialFile ? { file: initialFile, key: newIdempotencyKey() } : null,
  );
  const [name, setName] = useState('');
  const [precheck, setPrecheck] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });

  function choose(file: File | undefined) {
    if (!file) return;
    setPhase({ kind: 'idle' });
    if (file.size > MAX_UPLOAD_BYTES) {
      setSelection(null);
      setPrecheck(
        `"${file.name}" is ${formatBytes(file.size)}; the upload limit is ${formatBytes(MAX_UPLOAD_BYTES)}. Split the file or remove unused columns.`,
      );
      return;
    }
    setPrecheck(null);
    setSelection({ file, key: newIdempotencyKey() });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!selection) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase({ kind: 'uploading', loaded: 0, total: selection.file.size });
    try {
      const { response, created } = await uploadDataset(selection.file, {
        name: name.trim() || undefined,
        idempotencyKey: selection.key,
        signal: controller.signal,
        onProgress: (loaded, total) => setPhase({ kind: 'uploading', loaded, total }),
      });
      setPhase({ kind: 'processing', result: response, created });
      track({
        datasetId: response.dataset_id,
        versionId: response.version_id,
        label: name.trim() || selection.file.name,
      });
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === 'AbortError') {
        setPhase({ kind: 'idle' });
        return;
      }
      setPhase({ kind: 'failed', error: caught });
      notify({
        kind: 'error',
        title: `Upload failed: ${selection.file.name}`,
        body: uploadErrorDisplay(caught).message,
      });
    } finally {
      abortRef.current = null;
    }
  }

  function reset() {
    setSelection(null);
    setName('');
    setPhase({ kind: 'idle' });
  }

  if (phase.kind === 'processing') {
    const { result, created } = phase;
    return (
      <section aria-labelledby={`${inputId}-status`} className="flex flex-col gap-4">
        <h2 id={`${inputId}-status`} className="text-h2 text-text">
          {created ? 'Processing your data' : 'This file was already uploaded'}
        </h2>
        {created ? null : (
          <p className="text-body text-text-muted">
            It matches an existing version, so nothing new was stored.{' '}
            <Link
              to={`/datasets/${result.dataset_id}/overview?version=${result.version_id}`}
              className="text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
            >
              Open it
            </Link>
          </p>
        )}
        <ProcessingTimeline
          datasetId={result.dataset_id}
          versionId={result.version_id}
          jobId={result.job_id}
        />
        <div>
          <Button variant="secondary" size="sm" onClick={reset}>
            Upload another file
          </Button>
        </div>
      </section>
    );
  }

  const uploading = phase.kind === 'uploading';
  const percent = uploading && phase.total > 0 ? Math.round((phase.loaded / phase.total) * 100) : 0;

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
      <div
        onDragOver={(event: DragEvent) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event: DragEvent) => {
          event.preventDefault();
          setDragging(false);
          choose(event.dataTransfer.files[0]);
        }}
        className={`flex flex-col items-center gap-2 rounded-md border-2 border-dashed p-8 text-center ${dragging ? 'border-primary bg-primary/5' : 'border-border'}`}
      >
        <p className="text-body text-text">Drag a file here, or</p>
        <label
          htmlFor={inputId}
          className="text-body cursor-pointer rounded-md border border-border bg-surface px-4 py-2 font-medium text-text focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus hover:bg-surface-raised"
        >
          Choose a file
          <input
            id={inputId}
            type="file"
            accept={ACCEPT}
            disabled={uploading}
            onChange={(event: ChangeEvent<HTMLInputElement>) => choose(event.target.files?.[0])}
            className="sr-only"
          />
        </label>
        <p className="text-caption text-text-muted">
          CSV or Excel (.xlsx), up to {formatBytes(MAX_UPLOAD_BYTES)}. Macro-enabled workbooks are
          not accepted.
        </p>
      </div>

      {precheck ? (
        <p role="alert" className="text-caption text-error">
          {precheck}
        </p>
      ) : null}

      {selection ? (
        <p className="text-body text-text">
          Selected: <span className="font-medium">{selection.file.name}</span>{' '}
          <span className="text-text-muted">({formatBytes(selection.file.size)})</span>
        </p>
      ) : null}

      <label htmlFor={nameId} className="flex max-w-md flex-col gap-1">
        <span className="text-label text-text">Dataset name (optional)</span>
        <input
          id={nameId}
          type="text"
          maxLength={255}
          value={name}
          disabled={uploading}
          onChange={(event) => setName(event.target.value)}
          placeholder="Defaults to the file name"
          className="text-body rounded-sm border border-border bg-surface px-3 py-2 text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        />
      </label>

      {uploading ? (
        <div className="flex flex-col gap-2">
          <progress
            aria-label="Upload progress"
            value={phase.loaded}
            max={phase.total || 1}
            className="h-2 w-full max-w-md accent-primary"
          />
          <p className="text-caption text-text-muted" aria-live="polite">
            Uploading… {percent}% ({formatBytes(phase.loaded)} of {formatBytes(phase.total)})
          </p>
        </div>
      ) : null}

      {phase.kind === 'failed' ? <ErrorState {...uploadErrorDisplay(phase.error)} /> : null}

      <div className="flex gap-3">
        {uploading ? (
          <Button variant="secondary" onClick={() => abortRef.current?.abort()}>
            Cancel upload
          </Button>
        ) : (
          <Button type="submit" disabled={!selection}>
            {phase.kind === 'failed' ? 'Try again' : 'Upload'}
          </Button>
        )}
      </div>
    </form>
  );
}
