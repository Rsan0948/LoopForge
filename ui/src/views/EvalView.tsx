import { useCallback, useEffect, useState, type ReactElement } from "react";
import { ApiError, getEvalReport, type ConfigReportRow, type EvalReport } from "../api";
import { formatCost } from "../format";
import { ErrorBanner } from "../widgets";

// Eval report detail (PACS-016): the per-(config, task) outcome table for one
// operator-owned report. False success is the first-class metric this
// laboratory exists to measure, so those cells are visually distinct; Pareto
// frontier configurations are highlighted.

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function ReportRow({ row, pareto }: { row: ConfigReportRow; pareto: boolean }): ReactElement {
  return (
    <tr className={pareto ? "pareto-row" : undefined}>
      <td className="mono">
        {row.config_id}
        {pareto && (
          <>
            {" "}
            <span className="badge badge-green" title="on the Pareto frontier">
              pareto
            </span>
          </>
        )}
      </td>
      <td className="mono">{row.task_id}</td>
      <td className="mono">{row.trials}</td>
      <td className="mono">{formatRate(row.success_rate)}</td>
      <td
        className={
          row.false_successes > 0 ? "mono false-success-cell" : "mono false-success-cell zero"
        }
        title={`${row.false_successes}/${row.trials} trials passed the graders while the objective was not met`}
      >
        {formatRate(row.false_success_rate)}
      </td>
      <td className="mono">{formatCost(row.mean_cost_usd)}</td>
      <td className="mono">{row.mean_latency_seconds.toFixed(2)}s</td>
      <td className="mono">{row.mean_human_interventions.toFixed(2)}</td>
    </tr>
  );
}

export default function EvalView({ reportId }: { reportId: string }): ReactElement {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      setReport(await getEvalReport(reportId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, [reportId]);

  useEffect(() => {
    void load();
  }, [load]);

  const pareto = new Set(report?.pareto_config_ids ?? []);
  // Deterministic display order: grouped by configuration, then task.
  const rows = [...(report?.config_reports ?? [])].sort(
    (a, b) => a.config_id.localeCompare(b.config_id) || a.task_id.localeCompare(b.task_id),
  );

  return (
    <div className="stack">
      <p className="breadcrumb">
        <a href="#/evals">← eval reports</a>
      </p>
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      <section className="panel">
        <div className="panel-header">
          <h2 className="mono">{reportId}</h2>
          {report !== null && <span className="badge badge-muted">suite v{report.suite_version}</span>}
        </div>
        {report === null ? (
          error === null && <p className="muted">loading…</p>
        ) : (
          <>
            <p className="muted lock-line">
              suite lock <span className="mono">{report.lock_hash}</span>
            </p>
            <p className="muted">
              pareto frontier:{" "}
              {report.pareto_config_ids.length === 0 ? (
                "—"
              ) : (
                <span className="mono">{report.pareto_config_ids.join(", ")}</span>
              )}
            </p>
            <table className="sessions-table">
              <thead>
                <tr>
                  <th>config</th>
                  <th>task</th>
                  <th>trials</th>
                  <th>success</th>
                  <th>false success</th>
                  <th>mean cost</th>
                  <th>mean latency</th>
                  <th>mean interventions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <ReportRow
                    key={`${row.config_id}:${row.task_id}`}
                    row={row}
                    pareto={pareto.has(row.config_id)}
                  />
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  );
}
