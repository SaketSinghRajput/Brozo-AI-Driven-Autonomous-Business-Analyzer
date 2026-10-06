import { Card, ErrorState } from '../../components';
import type { CapabilityReport } from '../../contracts/generated/capability_report';
import { validateCapabilityReport } from '../../contracts/generated/validators';

function humanize(analysis: string): string {
  const text = analysis.replace(/[_-]+/g, ' ').trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * What this data supports (PRD-PLAN-002, UI-UX §30). Unavailable analyses are explained as
 * "Insufficient data" with the specific missing requirement and what would enable them -
 * styled as helpful information, not as an error. One entry per analysis with all its unmet
 * requirements (U-2). An available analysis the last dashboard run could not use (`attempted`,
 * e.g. no segmentation passed the quality gate) says so with the run's reason (U-4), so the
 * list never calls "available" what its widget calls "not available".
 */
export function CapabilityReportView({ report }: { report: unknown }) {
  if (!validateCapabilityReport(report)) {
    return <ErrorState message="The list of possible analyses couldn't be displayed." />;
  }
  const { available, unavailable: rules, attempted = [] } = report as CapabilityReport;
  const unavailable = new Map<string, typeof rules>();
  for (const item of rules) {
    unavailable.set(item.analysis, [...(unavailable.get(item.analysis) ?? []), item]);
  }
  const notUsable = new Map(attempted.map((item) => [item.analysis, item.message]));
  return (
    <Card>
      <section aria-labelledby="capabilities-heading" className="flex flex-col gap-4">
        <h2 id="capabilities-heading" className="text-h3 font-semibold">
          Analyses for this data
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div>
            <h3 className="text-caption font-semibold text-text-muted">Available</h3>
            <ul className="mt-2 flex flex-col gap-1">
              {available.map((analysis) => {
                const reason = notUsable.get(analysis);
                return (
                  <li key={analysis} className="flex gap-2">
                    <span aria-hidden="true" className={reason ? 'text-warning' : 'text-success'}>
                      {reason ? '!' : '✓'}
                    </span>
                    <div>
                      <span>{humanize(analysis)}</span>
                      {reason ? (
                        <>
                          <span className="text-caption ml-2 text-text-muted">
                            Attempted, not usable on this data
                          </span>
                          <p className="text-caption text-text-muted">{reason}</p>
                        </>
                      ) : (
                        <span className="sr-only">(available)</span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
          <div>
            <h3 className="text-caption font-semibold text-text-muted">Not available</h3>
            <ul className="mt-2 flex flex-col gap-3">
              {[...unavailable].map(([analysis, items]) => {
                const enable = [...new Set(items.flatMap((i) => i.would_enable ?? []))];
                return (
                  <li key={analysis} className="flex gap-2">
                    <span aria-hidden="true" className="text-text-muted">
                      ✗
                    </span>
                    <div>
                      <p className="font-medium">
                        {humanize(analysis)}
                        <span className="sr-only"> (not available)</span>
                        <span className="text-caption ml-2 font-normal text-text-muted">
                          Insufficient data
                        </span>
                      </p>
                      {items.map((item) => (
                        <p key={item.reason_code} className="text-caption text-text-muted">
                          {item.message}
                        </p>
                      ))}
                      {enable.length > 0 ? (
                        <p className="text-caption">
                          <span className="font-medium">What would enable it: </span>
                          {enable.join('; ')}
                        </p>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      </section>
    </Card>
  );
}
