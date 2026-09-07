import { useCallback, useEffect, useState, type ReactElement } from "react";
import { ApiError, listPolicies, type PolicyRecordInfo } from "../api";
import { navigateToPolicy } from "../App";
import { ErrorBanner, LifecycleBadge } from "../widgets";

// Candidate policy registry (PACS-017 M6): the operator-owned lifecycle of
// every registered policy record. The console reads the registry and offers
// exactly one write — the checkbox-gated promote dialog on the detail view;
// a candidate can never self-promote because no other write path exists.

const REFRESH_MS = 10000;

function PolicyRow({ record }: { record: PolicyRecordInfo }): ReactElement {
  return (
    <tr>
      <td>
        <a
          href={`#/policies/${encodeURIComponent(record.policy.policy_id)}`}
          className="mono"
          onClick={(event) => {
            event.preventDefault();
            navigateToPolicy(record.policy.policy_id);
          }}
        >
          {record.policy.policy_id}
        </a>
      </td>
      <td className="mono">v{record.policy.version}</td>
      <td>
        <LifecycleBadge lifecycle={record.lifecycle} />
      </td>
      <td className="mono">{record.evidence_basis}</td>
      <td className="muted">{record.note === "" ? "—" : record.note}</td>
    </tr>
  );
}

export default function PoliciesView(): ReactElement {
  const [records, setRecords] = useState<PolicyRecordInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setRecords(await listPolicies());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Poll like the sessions view: registrations/promotions happen via the
    // CLI, so the registry would otherwise go stale until a manual Refresh.
    const id = window.setInterval(() => void refresh(), REFRESH_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  return (
    <div className="stack">
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      <section className="panel">
        <div className="panel-header">
          <h2>Policy registry</h2>
          <button type="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
        {records === null ? (
          <p className="muted">loading…</p>
        ) : records.length === 0 ? (
          <p className="muted">
            no registered policies yet (register candidates with `loopforge policy`)
          </p>
        ) : (
          <table className="sessions-table">
            <thead>
              <tr>
                <th>policy</th>
                <th>version</th>
                <th>lifecycle</th>
                <th>evidence basis</th>
                <th>note</th>
              </tr>
            </thead>
            <tbody>
              {records.map((record) => (
                <PolicyRow
                  key={`${record.policy.policy_id}:v${record.policy.version}`}
                  record={record}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
