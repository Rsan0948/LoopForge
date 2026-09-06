import { useCallback, useEffect, useState, type FormEvent, type ReactElement } from "react";
import {
  ApiError,
  getPolicy,
  listEvalReports,
  promotePolicy,
  type PolicyRecordInfo,
} from "../api";
import { ErrorBanner, LifecycleBadge } from "../widgets";

// Policy record detail (PACS-017 M6): the full versioned knob bundle plus the
// lifecycle state and its referenced evidence basis. Promotion is an explicit
// operator action only — the inline dialog is checkbox-gated and requires a
// fresh evidence basis, mirroring the force-release confirmation pattern;
// only SHADOWED/BENCHMARKED records can be promoted (the domain transition
// table denies everything else with a 422).

// Only evidence-gathering states may transition to PROMOTED.
const PROMOTABLE = new Set(["shadowed", "benchmarked"]);

function EvidenceBasis({
  basis,
  evalReportIds,
}: {
  basis: string;
  evalReportIds: Set<string>;
}): ReactElement {
  // Evidence links: a basis naming a stored eval report id links straight to
  // that report; anything else (run reference, operator note) stays literal.
  if (evalReportIds.has(basis)) {
    return (
      <a href={`#/evals/${encodeURIComponent(basis)}`} className="mono">
        {basis}
      </a>
    );
  }
  return <span className="mono">{basis}</span>;
}

function PromoteControl({
  record,
  onPromoted,
  onError,
}: {
  record: PolicyRecordInfo;
  onPromoted: (updated: PolicyRecordInfo) => void;
  onError: (message: string) => void;
}): ReactElement {
  const [open, setOpen] = useState(false);
  const [basis, setBasis] = useState("");
  const [note, setNote] = useState("");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);

  const reset = (): void => {
    setOpen(false);
    setBasis("");
    setNote("");
    setChecked(false);
  };

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = basis.trim();
    if (!checked || trimmed === "" || busy) return;
    setBusy(true);
    promotePolicy(record.policy.policy_id, record.policy.version, trimmed, note.trim())
      .then((updated) => {
        reset();
        onPromoted(updated);
      })
      .catch((err: unknown) => {
        onError(err instanceof ApiError ? err.detail : String(err));
      })
      .finally(() => setBusy(false));
  };

  if (!PROMOTABLE.has(record.lifecycle)) {
    return (
      <span className="muted" title="only shadowed or benchmarked records can be promoted">
        promotion requires shadow or benchmark evidence first
      </span>
    );
  }
  if (!open) {
    return (
      <button
        type="button"
        className="danger"
        title="Promote this record to PROMOTED — an explicit, auditable operator action"
        onClick={() => setOpen(true)}
      >
        promote…
      </button>
    );
  }
  return (
    <form onSubmit={onSubmit} className="inline-form">
      <input
        autoFocus
        value={basis}
        onChange={(event) => setBasis(event.target.value)}
        placeholder="evidence basis (report id / run reference)"
      />
      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="operator note (optional)"
      />
      <label
        className="follow-toggle"
        title="Promotion is recorded with this evidence basis and cannot be undone — supersession is registering a NEW version"
      >
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => setChecked(event.target.checked)}
        />
        confirm promotion
      </label>
      <button type="submit" className="danger" disabled={!checked || basis.trim() === "" || busy}>
        confirm promote
      </button>
      <button type="button" onClick={reset}>
        cancel
      </button>
    </form>
  );
}

export default function PolicyView({ policyId }: { policyId: string }): ReactElement {
  const [record, setRecord] = useState<PolicyRecordInfo | null>(null);
  const [evalReportIds, setEvalReportIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      const [detail, reports] = await Promise.all([getPolicy(policyId), listEvalReports()]);
      setRecord(detail);
      setEvalReportIds(new Set(reports.map((report) => report.report_id)));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, [policyId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="stack">
      <p className="breadcrumb">
        <a href="#/policies">← policy registry</a>
      </p>
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      <section className="panel">
        <div className="panel-header">
          <h2 className="mono">
            {policyId}
            {record !== null ? ` v${record.policy.version}` : ""}
          </h2>
          {record !== null && <LifecycleBadge lifecycle={record.lifecycle} />}
        </div>
        {record === null ? (
          error === null && <p className="muted">loading…</p>
        ) : (
          <>
            <dl className="detail-grid mono">
              <dt>evidence basis</dt>
              <dd>
                <EvidenceBasis basis={record.evidence_basis} evalReportIds={evalReportIds} />
              </dd>
              <dt>note</dt>
              <dd>{record.note === "" ? "—" : record.note}</dd>
              <dt>context allocation</dt>
              <dd>
                floor {record.policy.context_allocation.floor_tokens} · ceiling{" "}
                {record.policy.context_allocation.ceiling_tokens} · reserve{" "}
                {record.policy.context_allocation.reserve_tokens} · step{" "}
                {record.policy.context_allocation.step_tokens} · low-util{" "}
                {record.policy.context_allocation.low_utilization_fraction}
              </dd>
              <dt>routing</dt>
              <dd>
                default tier {record.policy.routing.default_tier} · stall threshold{" "}
                {record.policy.routing.stall_escalation_threshold} · budget pressure{" "}
                {record.policy.routing.budget_pressure_remaining_fraction ?? "—"}
              </dd>
              <dt>verification</dt>
              <dd>
                {record.policy.verify_read_only_turns
                  ? "verify read-only turns"
                  : "read-only turns unverified"}
              </dd>
              <dt>worker count</dt>
              <dd>{record.policy.worker_count ?? "—"}</dd>
            </dl>
            <div className="controls-row">
              <PromoteControl
                record={record}
                onPromoted={(updated) => {
                  setRecord(updated);
                  setError(null);
                }}
                onError={setError}
              />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
