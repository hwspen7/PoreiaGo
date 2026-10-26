import { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, LoaderCircle, PlugZap } from 'lucide-react';
import { apiJSON } from '../../lib/api';
import './connectionStatusControl.css';

type ConnectionKind = 'model' | 'provider';
type ConnectionStatus = 'not_configured' | 'configured' | 'connected' | 'error';
type CheckResult = { status: ConnectionStatus; detail: string };

const statusLabel: Record<ConnectionStatus, string> = {
  not_configured: 'Not configured',
  configured: 'Configured',
  connected: 'Connected',
  error: 'Error',
};

export function ConnectionStatusControl({
  kind,
  name,
  configured,
  hint,
  revision,
}: {
  kind: ConnectionKind;
  name: string;
  configured: boolean;
  hint?: string;
  revision: number;
}) {
  const [result, setResult] = useState<CheckResult | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    setResult(null);
    setChecking(false);
  }, [kind, name, configured, revision]);

  const status: ConnectionStatus = configured
    ? result?.status ?? 'configured'
    : 'not_configured';

  async function check() {
    if (!configured || checking) return;
    setChecking(true);
    setResult(null);
    try {
      const checked = await apiJSON<CheckResult>('/api/connections/check', {
        method: 'POST',
        body: JSON.stringify({ kind, name, allow_remote: true }),
      });
      if (!['not_configured', 'configured', 'connected', 'error'].includes(checked.status)) {
        throw new Error('The server returned an invalid connection status.');
      }
      setResult(checked);
    } catch (exception) {
      setResult({
        status: 'error',
        detail: exception instanceof Error ? exception.message : 'Connection test failed.',
      });
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="connection-control">
      <div className="connection-control-actions">
        <span className={`infrastructure-status connection-status ${status}`} role="status">
          {status === 'connected' && <CheckCircle2 size={13} />}
          {status === 'error' && <AlertCircle size={13} />}
          {statusLabel[status]}
        </span>
        {configured && (
          <button
            className="connection-test-button"
            type="button"
            onClick={check}
            disabled={checking}
            aria-label={`Test ${kind} connection for ${name}`}
            title="Makes one real test request. External services may consume quota."
          >
            {checking ? <LoaderCircle size={13} className="connection-spinning" /> : <PlugZap size={13} />}
            {checking ? 'Testing' : 'Test'}
          </button>
        )}
      </div>
      {(result?.detail || (!configured && hint)) && (
        <span className="connection-detail" title={result?.detail ?? hint}>
          {result?.detail ?? hint}
        </span>
      )}
    </div>
  );
}
