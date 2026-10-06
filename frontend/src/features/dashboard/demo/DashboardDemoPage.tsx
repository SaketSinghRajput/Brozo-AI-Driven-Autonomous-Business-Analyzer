import { useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { EvidenceDrawerProvider } from '../../evidence/EvidenceDrawer';
import { DashboardScreen } from '../DashboardScreen';
import { DashboardDataProvider } from '../dataSource';
import { fixtureSource, type FixtureBundle } from '../fixtures/build';
import { demoFixtures } from '../fixtures';

/**
 * The dashboard screen over one fixture payload (demo route, tests). With
 * `semanticModelVersion`, filters re-query the backend like a real dataset does.
 */
export function DashboardView({
  bundle,
  semanticModelVersion,
}: {
  bundle: FixtureBundle;
  semanticModelVersion?: number;
}) {
  const source = useMemo(() => fixtureSource(bundle), [bundle]);
  return (
    <DashboardDataProvider source={source}>
      <EvidenceDrawerProvider>
        <DashboardScreen
          spec={bundle.spec}
          capabilities={bundle.capabilities}
          semanticModelVersion={semanticModelVersion}
          loadValues={(dimension) =>
            Promise.resolve({ values: bundle.dimensionValues?.[dimension] ?? [], truncated: false })
          }
        />
      </EvidenceDrawerProvider>
    </DashboardDataProvider>
  );
}

/**
 * Dev route /dashboards/demo: renders fixture payloads until the backend dashboard generator
 * exists (TASK-DASH-005, TASK-INTEGRATION-006). Selection lives in the URL (?fixture=...).
 */
export function DashboardDemoPage() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('fixture') ?? 'retail';
  const key = requested in demoFixtures ? requested : 'retail';
  const bundle = demoFixtures[key];

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-2">
        <h1 className="text-h1 font-semibold">Dashboard preview (sample data)</h1>
        <p className="text-caption text-text-muted">
          Rendered from fixture dashboard specs. The same components render every dataset; only the
          spec and its evidence differ.
        </p>
        <div role="group" aria-label="Sample dataset" className="flex flex-wrap gap-2">
          {Object.entries(demoFixtures).map(([name, fixture]) => (
            <button
              key={name}
              type="button"
              aria-pressed={name === key}
              onClick={() => setParams({ fixture: name })}
              className={`text-caption rounded-full border px-3 py-1 font-medium ${
                name === key
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border text-text-muted hover:bg-surface-raised'
              }`}
            >
              {fixture.label}
            </button>
          ))}
        </div>
      </header>
      <DashboardView key={key} bundle={bundle} />
    </div>
  );
}
