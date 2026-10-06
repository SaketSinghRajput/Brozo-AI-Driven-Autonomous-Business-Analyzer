import type { ReactNode } from 'react';
import { useAuth } from '../context/AuthContext';

/**
 * The API does not expose the model configuration yet, so these values mirror
 * backend/app/core/config.py LLMConfig defaults and docs/DECISIONS.md ADR-0007 (Ollama, accepted)
 * / ADR-0008 (qwen2.5:7b Q4_K_M, proposed). Replace with an API field when one exists.
 */
const LOCAL_MODEL = {
  runtime: 'Ollama (local model server on this instance)',
  model: 'qwen2.5:7b, 4-bit quantized (Q4_K_M)',
  status: 'Provisional default (ADR-0008 proposed)',
};

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-h2 text-text">{title}</h2>
      {children}
    </section>
  );
}

/** Privacy & system info (UI-UX §42, TASK-UI-031). Factual statements only; no absolutes. */
export function PrivacyPage() {
  const { can } = useAuth();
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <h1 className="text-h1 text-text">Privacy &amp; system info</h1>
      <p className="text-body text-text">
        Your data is processed by this self-hosted instance. No external AI services are used.
      </p>

      <Section title="Where your data is stored">
        <ul className="text-body flex list-disc flex-col gap-1 pl-5 text-text">
          <li>
            Uploaded files and the tables built from them are kept in this instance&apos;s file
            storage.
          </li>
          <li>
            Dataset records, profiles, sessions, jobs and the audit log are kept in this
            instance&apos;s PostgreSQL database.
          </li>
          <li>Data is separated by workspace; you only see the workspace you signed in to.</li>
        </ul>
      </Section>

      <Section title="AI models">
        <p className="text-body text-text">
          AI models run on this instance. The code is checked on every build for hosted AI provider
          libraries and addresses, which are not allowed.
        </p>
        {can('UPLOAD_DATASET') ? (
          <dl className="text-body grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <dt className="text-text-muted">Runtime</dt>
            <dd className="text-text">{LOCAL_MODEL.runtime}</dd>
            <dt className="text-text-muted">Language model</dt>
            <dd className="text-text">{LOCAL_MODEL.model}</dd>
            <dt className="text-text-muted">Status</dt>
            <dd className="text-text">{LOCAL_MODEL.status}</dd>
          </dl>
        ) : (
          <p className="text-caption text-text-muted">
            Model names are shown to Analysts and administrators.
          </p>
        )}
      </Section>

      <Section title="What is remembered">
        <p className="text-body text-text">
          Your datasets, their versions and profiles are kept until they are deleted. Your session
          lasts until you sign out or it expires. This browser only remembers which tips you
          dismissed.
        </p>
      </Section>

      <Section title="Usage tracking">
        <p className="text-body text-text">
          This app loads no third-party scripts, fonts or analytics, and sends no telemetry.
        </p>
      </Section>

      <Section title="Optional non-local components">
        <p className="text-body text-text">None enabled.</p>
      </Section>
    </div>
  );
}
