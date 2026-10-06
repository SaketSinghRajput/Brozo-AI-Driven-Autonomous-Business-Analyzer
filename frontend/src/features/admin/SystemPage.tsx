import { Skeleton } from '../../components/Surfaces';
import { useAuth } from '../../context/AuthContext';

/** System / admin area (UI-UX §40-41): health, jobs, models. Real data is TASK-INFRA-004+. */
export function SystemPage() {
  const { can } = useAuth();
  if (!can('VIEW_AUDIT')) {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-h1 text-text">System</h1>
        <p role="alert" className="text-body text-text">
          This area requires administrator permission.
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-h1 text-text">System</h1>
      <Skeleton variant="table" rows={4} />
    </div>
  );
}
