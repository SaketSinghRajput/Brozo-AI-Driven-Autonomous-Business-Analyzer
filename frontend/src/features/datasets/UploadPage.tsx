import { useState } from 'react';
import { useAuth } from '../../context/AuthContext';
import { requiredRoleText } from '../../context/permissions';
import { SampleData } from '../onboarding/SampleData';
import { DatasetUploader } from './DatasetUploader';

/** /datasets/upload (UI-UX §13). Viewers get the reason instead of the form (UI-UX §43). */
export function UploadPage() {
  const { can } = useAuth();
  // A picked sample remounts the uploader pre-filled (fresh Idempotency-Key per pick).
  const [sample, setSample] = useState<{ file: File; pick: number } | null>(null);

  if (!can('UPLOAD_DATASET')) {
    return (
      <div className="flex max-w-3xl flex-col gap-4">
        <h1 className="text-h1 text-text">Upload dataset</h1>
        <p role="alert" className="text-body text-text">
          Uploading data is not available for your role. {requiredRoleText('UPLOAD_DATASET')}.
        </p>
      </div>
    );
  }
  return (
    <div className="flex max-w-3xl flex-col gap-8">
      <div className="flex flex-col gap-4">
        <h1 className="text-h1 text-text">Upload dataset</h1>
        <DatasetUploader key={sample?.pick ?? 0} initialFile={sample?.file} />
      </div>
      <SampleData onPick={(file) => setSample((s) => ({ file, pick: (s?.pick ?? 0) + 1 }))} />
    </div>
  );
}
