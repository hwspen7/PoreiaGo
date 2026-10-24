import { useMemo, useState } from 'react';
import {
  Activity,
  BrainCircuit,
  CheckCircle2,
  CloudSun,
  KeyRound,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react';
import type { Health, ModelProfile, Provider } from './api';
import { setAuthToken } from './api';
import './modelsProvidersOverview.css';

type ViewFilter = 'ready' | 'all';
const titleCase = (name: string) =>
  name.replace(/[_-]+/g, ' ').replace(/\b\w/g, (s) => s.toUpperCase());
const providerLabel = (p: Provider) => titleCase(p.capability);
const capabilityCategory = (p: Provider) => {
  const k = p.capability.toLowerCase();
  if (/route|transit|rail|driving|cycling|walking|travel_time|flight/.test(k))
    return 'Routes & transport';
  if (/weather|air_quality|climate|environment/.test(k)) return 'Weather & conditions';
  if (/accommodation|hotel|price|currency|fx|offer/.test(k)) return 'Prices & stays';
  return 'Places & local information';
};
const statusText = (p: Provider) =>
  p.configured ? 'Configured' : p.requires_key ? 'API key required' : 'Unavailable';

export function ModelsProvidersOverview({
  health,
  onRefresh,
}: {
  health: Health | null;
  onRefresh: () => void;
}) {
  const [modelsFilter, setModelsFilter] = useState<ViewFilter>('ready');
  const [providersFilter, setProvidersFilter] = useState<ViewFilter>('ready');
  const [query, setQuery] = useState('');
  const [token, setToken] = useState('');
  const [tokenApplied, setTokenApplied] = useState(false);
  const configuredModels = (health?.models ?? []).filter((m) => m.configured);
  const configuredProviders = (health?.providers ?? []).filter((p) => p.configured);
  const models = useMemo(
    () =>
      (health?.models ?? []).filter(
        (m) =>
          (modelsFilter === 'all' || m.configured) &&
          `${m.name} ${m.provider} ${m.role} ${m.model}`
            .toLowerCase()
            .includes(query.toLowerCase()),
      ),
    [health?.models, modelsFilter, query],
  );
  const providers = useMemo(
    () =>
      (health?.providers ?? []).filter(
        (p) =>
          (providersFilter === 'all' || p.configured) &&
          `${p.capability} ${p.provider}`.toLowerCase().includes(query.toLowerCase()),
      ),
    [health?.providers, providersFilter, query],
  );
  const groups = [
    'Places & local information',
    'Routes & transport',
    'Weather & conditions',
    'Prices & stays',
  ];
  return (
    <div className="dashboard infrastructure-page">
      <header className="page-heading infrastructure-heading">
        <div className="eyebrow">SYSTEM CONNECTIONS</div>
        <div className="infrastructure-heading-row">
          <div>
            <h1>Models & Data Services</h1>
            <p>See which AI models and travel data services are configured for PoreiaGo</p>
          </div>
          <button type="button" className="infrastructure-refresh" onClick={onRefresh}>
            <RefreshCw size={15} /> Refresh status
          </button>
        </div>
      </header>
      <div className="infrastructure-overview" aria-label="Configuration summary">
        <div className="infrastructure-summary">
          <BrainCircuit size={19} />
          <div>
            <strong>{configuredModels.length}</strong>
            <span>Model profiles configured</span>
          </div>
        </div>
        <div className="infrastructure-summary">
          <CloudSun size={19} />
          <div>
            <strong>{configuredProviders.length}</strong>
            <span>Data services configured</span>
          </div>
        </div>
        <div className="infrastructure-summary">
          <Activity size={19} />
          <div>
            <strong>{health?.online_enabled ? 'On' : 'Off'}</strong>
            <span>Online data access</span>
          </div>
        </div>
      </div>
      <div className="infrastructure-context">
        <ShieldCheck size={16} />
        <span>
          Configured means credentials or local settings are present, not that a live connection has
          been verified
        </span>
      </div>
      <label className="infrastructure-search">
        <Search size={17} />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Find a model or data service"
          aria-label="Find a model or data service"
        />
      </label>
      <div className="infrastructure-columns">
        <section className="infrastructure-panel">
          <div className="infrastructure-panel-header">
            <span className="infrastructure-panel-icon">
              <BrainCircuit size={19} />
            </span>
            <div>
              <h2>AI models</h2>
              <p>Models available for planning and reasoning</p>
            </div>
          </div>
          <div className="infrastructure-filter" aria-label="Filter model profiles">
            <button
              className={modelsFilter === 'ready' ? 'active' : ''}
              type="button"
              aria-pressed={modelsFilter === 'ready'}
              onClick={() => setModelsFilter('ready')}
            >
              Configured
            </button>
            <button
              className={modelsFilter === 'all' ? 'active' : ''}
              type="button"
              aria-pressed={modelsFilter === 'all'}
              onClick={() => setModelsFilter('all')}
            >
              All profiles
            </button>
          </div>
          <div className="infrastructure-rows">
            {models.map((m: ModelProfile) => (
              <div className="infrastructure-item" key={m.name}>
                <div className="infrastructure-item-main">
                  <strong>{m.name}</strong>
                  <span>
                    {m.role} · {m.model}
                  </span>
                  <small>{titleCase(m.provider)}</small>
                </div>
                <span
                  className={
                    'infrastructure-status ' + (m.configured ? 'available' : 'unavailable')
                  }
                >
                  {m.configured ? <CheckCircle2 size={13} /> : null}
                  {m.provider === 'local-hf' && !m.configured
                    ? 'On demand'
                    : m.configured
                      ? 'Configured'
                      : 'Not configured'}
                </span>
              </div>
            ))}
            {!models.length && (
              <div className="infrastructure-empty">No model profiles match this view</div>
            )}
          </div>
        </section>
        <section className="infrastructure-panel">
          <div className="infrastructure-panel-header">
            <span className="infrastructure-panel-icon">
              <CloudSun size={19} />
            </span>
            <div>
              <h2>Travel data services</h2>
              <p>External information used when a query needs it</p>
            </div>
          </div>
          <div className="infrastructure-filter" aria-label="Filter data services">
            <button
              className={providersFilter === 'ready' ? 'active' : ''}
              type="button"
              aria-pressed={providersFilter === 'ready'}
              onClick={() => setProvidersFilter('ready')}
            >
              Configured
            </button>
            <button
              className={providersFilter === 'all' ? 'active' : ''}
              type="button"
              aria-pressed={providersFilter === 'all'}
              onClick={() => setProvidersFilter('all')}
            >
              All services
            </button>
          </div>
          <div className="infrastructure-rows">
            {groups.map((group) => {
              const matches = providers.filter((p) => capabilityCategory(p) === group);
              if (!matches.length) return null;
              return (
                <div key={group} className="infrastructure-group">
                  <h3>{group}</h3>
                  {matches.map((p) => (
                    <div className="infrastructure-item" key={p.capability}>
                      <div className="infrastructure-item-main">
                        <strong>{providerLabel(p)}</strong>
                        <span>{p.provider}</span>
                      </div>
                      <span
                        className={
                          'infrastructure-status ' + (p.configured ? 'available' : 'unavailable')
                        }
                      >
                        {p.configured ? <CheckCircle2 size={13} /> : null}
                        {statusText(p)}
                      </span>
                    </div>
                  ))}
                </div>
              );
            })}
            {!providers.length && (
              <div className="infrastructure-empty">No data services match this view</div>
            )}
          </div>
        </section>
      </div>
      <section className="infrastructure-access">
        <div className="infrastructure-access-icon">
          <KeyRound size={19} />
        </div>
        <div className="infrastructure-access-copy">
          <h2>Remote access token</h2>
          <p>Only needed when your PoreiaGo server requires a bearer token for API requests</p>
        </div>
        <form
          className="infrastructure-token-form"
          onSubmit={(e) => {
            e.preventDefault();
            setAuthToken(token);
            setTokenApplied(true);
            onRefresh();
          }}
        >
          <input
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setTokenApplied(false);
            }}
            autoComplete="off"
            placeholder="Bearer token (optional)"
            aria-label="Remote access token"
          />
          <button type="submit">Apply</button>
          {tokenApplied && <span role="status">Updated for this browser session</span>}
        </form>
      </section>
      <div className="infrastructure-footer">
        <SlidersHorizontal size={15} /> Model profiles are configured on the server; provider
        credentials remain in your environment settings
      </div>
    </div>
  );
}
