import { useCallback, useEffect, useState, type ReactElement } from "react";
import {
  ApiError,
  getBenchmarkSuite,
  listEvalReports,
  type BenchmarkSuiteInfo,
  type EvalReportSummary,
} from "../api";
import { navigateToEval } from "../App";
import { formatTime } from "../format";
import { ErrorBanner } from "../widgets";

// Eval reports (PACS-016): read-only exposure of operator-owned artifacts.
// The benchmark-suite header carries both lock hashes — the operator-visible
// proof the benchmark definition the reports were measured against is locked.

function shortHash(hash: string): string {
  return hash.slice(0, 12);
}

function SuitePanel({ suite }: { suite: BenchmarkSuiteInfo | null }): ReactElement {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Benchmark suite</h2>
        {suite !== null && <span className="badge badge-muted">v{suite.version}</span>}
      </div>
      {suite === null ? (
        <p className="muted">loading…</p>
      ) : (
        <>
          <p className="muted lock-line">
            suite lock <span className="mono">{suite.lock_hash}</span>
          </p>
          <p className="muted lock-line">
            content lock <span className="mono">{suite.content_lock}</span>
          </p>
          <table className="sessions-table">
            <thead>
              <tr>
                <th>task</th>
                <th>category</th>
                <th>sandbox</th>
                <th>live eligible</th>
                <th>graders</th>
              </tr>
            </thead>
            <tbody>
              {suite.tasks.map((task) => (
                <tr key={task.task_id}>
                  <td className="mono">{task.task_id}</td>
                  <td>{task.category}</td>
                  <td className="mono">{task.sandbox_mode}</td>
                  <td>{task.live_eligible ? "yes" : "no"}</td>
                  <td className="muted mono">{task.grader_ids.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

export default function EvalsView(): ReactElement {
  const [suite, setSuite] = useState<BenchmarkSuiteInfo | null>(null);
  const [reports, setReports] = useState<EvalReportSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const [suiteInfo, reportList] = await Promise.all([getBenchmarkSuite(), listEvalReports()]);
      setSuite(suiteInfo);
      setReports(reportList);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="stack">
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      <SuitePanel suite={suite} />
      <section className="panel">
        <div className="panel-header">
          <h2>Eval reports</h2>
          <button type="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
        {reports === null ? (
          <p className="muted">loading…</p>
        ) : reports.length === 0 ? (
          <p className="muted">no eval reports stored yet (run `loopforge eval`)</p>
        ) : (
          <table className="sessions-table">
            <thead>
              <tr>
                <th>report</th>
                <th>created</th>
                <th>suite</th>
                <th>lock</th>
                <th>configs</th>
                <th>tasks</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((report) => (
                <tr key={report.report_id}>
                  <td>
                    <a
                      href={`#/evals/${encodeURIComponent(report.report_id)}`}
                      className="mono"
                      onClick={(event) => {
                        event.preventDefault();
                        navigateToEval(report.report_id);
                      }}
                    >
                      {report.report_id}
                    </a>
                  </td>
                  <td className="mono">{formatTime(report.created_at)}</td>
                  <td className="mono">v{report.suite_version}</td>
                  <td className="mono muted" title={report.lock_hash}>
                    {shortHash(report.lock_hash)}
                  </td>
                  <td className="mono">{report.config_ids.join(", ")}</td>
                  <td className="mono">{report.task_ids.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
