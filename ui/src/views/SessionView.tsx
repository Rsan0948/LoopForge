import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import {
  ApiError,
  approveAction,
  explainNode,
  followUp,
  forceRelease,
  getArtifacts,
  getEvents,
  getLineage,
  getProvenance,
  getSession,
  pauseSession,
  rejectAction,
  resumeSession,
  rollbackRun,
  sendInstruction,
  sessionWsUrl,
  startSession,
  stopSession,
  TERMINAL_STATUSES,
  type ArtifactInfo,
  type ProvenanceExplanation,
  type ProvenanceGraph,
  type ProvenanceNode,
  type SessionDetail,
  type SessionLineage,
} from "../api";
import { navigateToSession } from "../App";
import DiffViewer from "../DiffViewer";
import { describeEvent, type EventEnvelope } from "../events";
import { formatClock, formatCost, formatTime } from "../format";
import { ErrorBanner, StatusBadge } from "../widgets";

type WsState = "connecting" | "open" | "reconnecting" | "closed";

const WS_CLOSE_UNKNOWN_RUN = 4404;
const DETAIL_REFRESH_MS = 250;
const TOAST_MS = 6000;

function EventRow({ envelope }: { envelope: EventEnvelope }): ReactElement {
  const { label, summary } = describeEvent(envelope);
  return (
    <details className="event-row">
      <summary>
        <span className="mono event-seq">#{envelope.event.sequence}</span>
        <span className="mono event-time">{formatClock(envelope.event.occurred_at)}</span>
        <span className={`event-label event-${envelope.event_type}`}>{label}</span>
        <span className="event-summary">{summary}</span>
      </summary>
      <pre className="event-raw">{JSON.stringify(envelope.event, null, 2)}</pre>
    </details>
  );
}

function ProvenanceNodeRow({
  node,
  selected,
  onExplain,
}: {
  node: ProvenanceNode;
  selected: boolean;
  onExplain: (nodeId: string) => void;
}): ReactElement {
  return (
    <button
      type="button"
      className={`prov-node${selected ? " prov-selected" : ""}`}
      title={`${node.event_type} — click for the evidence chain`}
      onClick={() => onExplain(node.node_id)}
    >
      <span className="mono event-seq">#{node.sequence}</span>
      <span className="badge badge-muted">{node.kind}</span>
      <span className="prov-summary">{node.summary}</span>
    </button>
  );
}

export default function SessionView({ runId }: { runId: string }): ReactElement {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactInfo[]>([]);
  const [wsState, setWsState] = useState<WsState>("connecting");
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);

  const [lineage, setLineage] = useState<SessionLineage | null>(null);
  const [graph, setGraph] = useState<ProvenanceGraph | null>(null);
  const [provOpen, setProvOpen] = useState(false);
  const [explained, setExplained] = useState<ProvenanceExplanation | null>(null);

  const [stopOpen, setStopOpen] = useState(false);
  const [stopSummary, setStopSummary] = useState("");
  const [forceOpen, setForceOpen] = useState(false);
  const [forceSummary, setForceSummary] = useState("");
  const [forceChecked, setForceChecked] = useState(false);
  const [amendOpen, setAmendOpen] = useState(false);
  const [amendText, setAmendText] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [instruction, setInstruction] = useState("");

  const streamRef = useRef<HTMLDivElement | null>(null);
  // Monotonic guard against out-of-order REST responses: two in-flight
  // getSession calls can resolve older-last, and for a terminal run no
  // further event would ever correct the overwritten (stale) detail.
  const detailVersionRef = useRef(-1);
  const applyDetail = useCallback((fresh: SessionDetail) => {
    if (fresh.version < detailVersionRef.current) return;
    detailVersionRef.current = fresh.version;
    setDetail(fresh);
  }, []);

  // -- data flow: REST initial load, WS live stream, REST reconcile ----------

  useEffect(() => {
    const disposed = { current: false };
    const latestSequence = { current: 0 };
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let detailTimer: number | null = null;
    let artifactTimer: number | null = null;
    let attempts = 0;

    const refreshDetail = (): void => {
      void getSession(runId)
        .then((fresh) => {
          if (!disposed.current) applyDetail(fresh);
        })
        .catch(() => undefined);
    };

    const refreshArtifacts = (): void => {
      void getArtifacts(runId)
        .then((fresh) => {
          if (!disposed.current) setArtifacts(fresh);
        })
        .catch(() => undefined);
    };

    const refreshLineage = (): void => {
      void getLineage(runId)
        .then((fresh) => {
          if (!disposed.current) setLineage(fresh);
        })
        .catch(() => undefined);
    };

    const scheduleDetailRefresh = (): void => {
      if (detailTimer !== null) return;
      detailTimer = window.setTimeout(() => {
        detailTimer = null;
        refreshDetail();
      }, DETAIL_REFRESH_MS);
    };

    const scheduleArtifactRefresh = (): void => {
      if (artifactTimer !== null) return;
      artifactTimer = window.setTimeout(() => {
        artifactTimer = null;
        refreshArtifacts();
      }, DETAIL_REFRESH_MS);
    };

    // Sequences are monotonic per run, so "greater than last seen" dedupes
    // both live frames and REST reconciliation pages.
    const ingest = (batch: EventEnvelope[]): void => {
      const fresh = batch
        .filter((envelope) => envelope.event.sequence > latestSequence.current)
        .sort((a, b) => a.event.sequence - b.event.sequence);
      if (fresh.length === 0) return;
      latestSequence.current = fresh[fresh.length - 1].event.sequence;
      if (!disposed.current) setEvents((prev) => [...prev, ...fresh]);
      scheduleDetailRefresh();
      if (fresh.some((envelope) => envelope.event_type === "ArtifactRecorded")) {
        scheduleArtifactRefresh();
      }
    };

    const connect = (): void => {
      if (disposed.current) return;
      setWsState(attempts === 0 ? "connecting" : "reconnecting");
      ws = new WebSocket(sessionWsUrl(runId));
      ws.onopen = () => {
        attempts = 0;
        if (!disposed.current) setWsState("open");
      };
      ws.onmessage = (message: MessageEvent<string>) => {
        try {
          ingest([JSON.parse(message.data) as EventEnvelope]);
        } catch {
          // Ignore malformed frames; the stream is authoritative but
          // reconciliation will close any gap.
        }
      };
      ws.onclose = (event: CloseEvent) => {
        if (disposed.current) return;
        if (event.code === WS_CLOSE_UNKNOWN_RUN) {
          setWsState("closed");
          setFatalError("unknown run — the server closed the event stream");
          return;
        }
        setWsState("reconnecting");
        const delay = Math.min(1000 * 2 ** attempts, 15000);
        attempts += 1;
        reconnectTimer = window.setTimeout(() => {
          // D6: reconcile from the durable store after the last seen
          // sequence before resuming the live stream.
          void getEvents(runId, latestSequence.current)
            .then((page) => ingest(page.events))
            .catch(() => undefined)
            .finally(connect);
        }, delay);
      };
    };

    void (async () => {
      try {
        const [initial, page, initialArtifacts] = await Promise.all([
          getSession(runId),
          getEvents(runId, 0),
          getArtifacts(runId),
        ]);
        if (disposed.current) return;
        applyDetail(initial);
        setArtifacts(initialArtifacts);
        ingest(page.events);
        refreshLineage();
        connect();
      } catch (err) {
        if (disposed.current) return;
        if (err instanceof ApiError && err.status === 404) {
          setWsState("closed");
          setFatalError(err.detail);
        } else {
          setToast(err instanceof ApiError ? err.detail : String(err));
          connect();
        }
      }
    })();

    return () => {
      disposed.current = true;
      ws?.close();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (detailTimer !== null) window.clearTimeout(detailTimer);
      if (artifactTimer !== null) window.clearTimeout(artifactTimer);
    };
  }, [runId]);

  // -- provenance: derived on demand, refreshed with the stream while open -------

  const eventCount = events.length;
  useEffect(() => {
    if (!provOpen) return;
    let disposed = false;
    void getProvenance(runId)
      .then((fresh) => {
        if (!disposed) setGraph(fresh);
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
    };
  }, [runId, provOpen, eventCount]);

  // -- auto-scroll -------------------------------------------------------------

  useEffect(() => {
    const node = streamRef.current;
    if (follow && node !== null) {
      node.scrollTop = node.scrollHeight;
    }
  }, [events, follow]);

  // -- transient error toast -----------------------------------------------------

  useEffect(() => {
    if (toast === null) return;
    const id = window.setTimeout(() => setToast(null), TOAST_MS);
    return () => window.clearTimeout(id);
  }, [toast]);

  // -- commands (POST only; the server response + stream are authoritative) ----

  const runCommand = useCallback(
    (command: () => Promise<unknown>): void => {
      void (async () => {
        try {
          await command();
          // The server is authoritative; re-pull projections after every
          // command (a rollback's effect shows up in the next snapshot).
          const [freshDetail, freshArtifacts, freshLineage] = await Promise.all([
            getSession(runId),
            getArtifacts(runId),
            getLineage(runId),
          ]);
          applyDetail(freshDetail);
          setArtifacts(freshArtifacts);
          setLineage(freshLineage);
        } catch (err) {
          setToast(err instanceof ApiError ? err.detail : String(err));
        }
      })();
    },
    [runId],
  );

  const terminal = detail !== null && TERMINAL_STATUSES.has(detail.status);
  const canFollowUp = terminal && detail.managed;
  const canStart = detail !== null && !terminal && !detail.driving && detail.managed;
  const canPause = detail !== null && !terminal && detail.driving;
  const canResume = detail !== null && !terminal && !detail.driving && detail.managed;
  const canStop = detail !== null && !terminal;
  const canInstruct = detail !== null && !terminal;
  const canForceRelease = detail !== null && !terminal && !detail.driving;
  const pending = detail !== null && detail.waiting_for_approval ? detail.pending_approval : null;

  const onStop = (event: FormEvent): void => {
    event.preventDefault();
    const summary = stopSummary.trim();
    setStopOpen(false);
    setStopSummary("");
    runCommand(() => stopSession(runId, summary === "" ? undefined : summary));
  };

  const onForceRelease = (event: FormEvent): void => {
    event.preventDefault();
    if (!forceChecked) return;
    const summary = forceSummary.trim();
    setForceOpen(false);
    setForceSummary("");
    setForceChecked(false);
    runCommand(() => forceRelease(runId, summary === "" ? undefined : summary));
  };

  const onExplainNode = (nodeId: string): void => {
    if (explained?.node.node_id === nodeId) {
      setExplained(null);
      return;
    }
    void explainNode(runId, nodeId)
      .then(setExplained)
      .catch((err) => setToast(err instanceof ApiError ? err.detail : String(err)));
  };

  const onFollowUp = (): void => {
    void (async () => {
      try {
        const { run_id } = await followUp(runId);
        navigateToSession(run_id);
      } catch (err) {
        setToast(err instanceof ApiError ? err.detail : String(err));
      }
    })();
  };

  const onReject = (event: FormEvent): void => {
    event.preventDefault();    if (pending === null || !rejectReason.trim()) return;
    const reason = rejectReason.trim();
    setRejectOpen(false);
    setRejectReason("");
    runCommand(() => rejectAction(runId, pending.action_id, reason));
  };

  const onInstruct = (event: FormEvent): void => {
    event.preventDefault();
    if (!instruction.trim()) return;
    const text = instruction.trim();
    setInstruction("");
    runCommand(() => sendInstruction(runId, text));
  };

  const onAmend = (event: FormEvent): void => {
    event.preventDefault();
    if (!amendText.trim()) return;
    const text = amendText.trim();
    setAmendOpen(false);
    setAmendText("");
    runCommand(() => sendInstruction(runId, text, true));
  };

  const budget = detail?.budget ?? null;

  return (
    <div className="stack">
      <nav className="breadcrumb">
        <a href="#/sessions">← all sessions</a>
      </nav>

      {fatalError !== null && <ErrorBanner message={fatalError} />}
      {toast !== null && <ErrorBanner message={toast} onDismiss={() => setToast(null)} />}

      <section className="panel">
        {detail === null ? (
          <p className="muted">{fatalError === null ? "loading…" : "session unavailable"}</p>
        ) : (
          <>
            <div className="panel-header">
              <h2 className="mono">{detail.run_id}</h2>
              <StatusBadge status={detail.status} />
              <span className={`ws-dot ws-${wsState}`} title={`event stream: ${wsState}`}>
                {wsState === "open" ? "live" : wsState}
              </span>
            </div>
            <p className="objective">{detail.objective}</p>
            {detail.driver_error !== undefined && (
              <ErrorBanner message={`driver error: ${detail.driver_error}`} />
            )}
            <dl className="detail-grid mono">
              <div>
                <dt>repository</dt>
                <dd>{detail.repository ?? "—"}</dd>
              </div>
              <div>
                <dt>iteration</dt>
                <dd>
                  {detail.iteration}
                  {budget !== null ? ` / ${budget.max_iterations}` : ""}
                </dd>
              </div>
              <div>
                <dt>cost</dt>
                <dd>
                  {formatCost(detail.cost_usd)}
                  {budget !== null ? ` / ${formatCost(budget.max_cost_usd)}` : ""}
                </dd>
              </div>
              <div>
                <dt>tokens</dt>
                <dd>
                  {detail.total_tokens.toLocaleString()} ({detail.input_tokens.toLocaleString()} in /{" "}
                  {detail.output_tokens.toLocaleString()} out)
                  {budget?.max_total_tokens != null ? ` / ${budget.max_total_tokens.toLocaleString()}` : ""}
                </dd>
              </div>
              <div>
                <dt>started</dt>
                <dd>{formatTime(detail.started_at)}</dd>
              </div>
              <div>
                <dt>last event</dt>
                <dd>{formatTime(detail.last_occurred_at)}</dd>
              </div>
              <div>
                <dt>flags</dt>
                <dd>
                  {detail.driving ? "driving " : ""}
                  {detail.managed ? "managed" : "unmanaged"}
                  {detail.stop_reason !== null ? ` · stop: ${detail.stop_reason}` : ""}
                </dd>
              </div>
              <div>
                <dt>lineage</dt>
                <dd className="lineage-links">
                  {lineage === null ||
                  (lineage.parent_run_id === null && lineage.children.length === 0) ? (
                    "—"
                  ) : (
                    <>
                      {lineage.parent_run_id !== null && (
                        <button
                          type="button"
                          className="link-button"
                          title="parent run (this session was seeded as its follow-up)"
                          onClick={() => {
                            const parent = lineage.parent_run_id;
                            if (parent !== null) navigateToSession(parent);
                          }}
                        >
                          ← parent
                        </button>
                      )}
                      {lineage.children.map((child) => (
                        <button
                          key={child}
                          type="button"
                          className="link-button"
                          title="follow-up run seeded from this one"
                          onClick={() => navigateToSession(child)}
                        >
                          child →
                        </button>
                      ))}
                    </>
                  )}
                </dd>
              </div>
              <div>
                <dt>last verification</dt>
                <dd>
                  {detail.last_verification === null
                    ? "—"
                    : `${detail.last_verification_passed === true ? "pass" : "fail"}${
                        detail.last_verification_score !== null
                          ? ` (${detail.last_verification_score.toFixed(2)})`
                          : ""
                      }: ${detail.last_verification}`}
                </dd>
              </div>
            </dl>
            {detail.operator_instructions.length > 0 && (
              <p className="muted">
                operator instructions ({detail.operator_instructions.length}) — latest: “
                {detail.operator_instructions[detail.operator_instructions.length - 1]}”
              </p>
            )}
          </>
        )}
      </section>

      {pending !== null && detail !== null && (
        <section className="panel panel-amber">
          <div className="panel-header">
            <h2>⚠ approval required</h2>
          </div>
          <p>{pending.reason}</p>
          {detail.current_proposal !== null && (
            <>
              <p className="mono">
                tool: <strong>{detail.current_proposal.tool_name}</strong>
              </p>
              <pre className="proposal-args">
                {JSON.stringify(detail.current_proposal.arguments, null, 2)}
              </pre>
            </>
          )}
          {rejectOpen ? (
            <form onSubmit={onReject} className="inline-form">
              <input
                autoFocus
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="rejection reason (required)"
              />
              <button type="submit" className="danger" disabled={!rejectReason.trim()}>
                confirm reject
              </button>
              <button type="button" onClick={() => setRejectOpen(false)}>
                cancel
              </button>
            </form>
          ) : (
            <div className="button-row">
              <button
                type="button"
                className="primary"
                onClick={() => runCommand(() => approveAction(runId, pending.action_id))}
              >
                approve
              </button>
              <button type="button" className="danger" onClick={() => setRejectOpen(true)}>
                reject…
              </button>
            </div>
          )}
          {detail.last_approval_rejection !== null && (
            <p className="muted">last rejection: {detail.last_approval_rejection}</p>
          )}
        </section>
      )}

      <section className="panel">
        <div className="panel-header">
          <h2>
            provenance
            {graph !== null && provOpen
              ? ` (${graph.nodes.length} nodes / ${graph.edges.length} edges)`
              : ""}
          </h2>
          <button type="button" onClick={() => setProvOpen((open) => !open)}>
            {provOpen ? "hide" : "show"}
          </button>
        </div>
        {provOpen && (
          <>
            {graph === null ? (
              <p className="muted">deriving graph…</p>
            ) : (
              <div className="prov-list">
                {graph.nodes.map((node) => (
                  <ProvenanceNodeRow
                    key={node.node_id}
                    node={node}
                    selected={explained?.node.node_id === node.node_id}
                    onExplain={onExplainNode}
                  />
                ))}
              </div>
            )}
            {explained !== null && (
              <div className="explain-panel">
                <p className="muted">
                  why did <span className="mono">{explained.node.node_id}</span> happen? causal
                  spine → focus → outcomes
                </p>
                <div className="prov-list">
                  {explained.chain.map((node) => (
                    <ProvenanceNodeRow
                      key={node.node_id}
                      node={node}
                      selected={false}
                      onExplain={onExplainNode}
                    />
                  ))}
                  <div className="prov-node prov-selected">
                    <span className="mono event-seq">#{explained.node.sequence}</span>
                    <span className="badge badge-active">{explained.node.kind}</span>
                    <span className="prov-summary">{explained.node.summary}</span>
                  </div>
                  {explained.outcomes.map((node) => (
                    <ProvenanceNodeRow
                      key={node.node_id}
                      node={node}
                      selected={false}
                      onExplain={onExplainNode}
                    />
                  ))}
                </div>
                {Object.keys(explained.node.attributes).length > 0 && (
                  <pre className="event-raw">
                    {JSON.stringify(explained.node.attributes, null, 2)}
                  </pre>
                )}
                {explained.supporting.length > 0 && (
                  <p className="muted">
                    supporting evidence:{" "}
                    {explained.supporting.map((node) => node.node_id).join(", ")}
                  </p>
                )}
              </div>
            )}
          </>
        )}
      </section>

      <section className="panel">
        <div className="controls-row">
          <button type="button" disabled={!canStart} onClick={() => runCommand(() => startSession(runId))}>
            start
          </button>
          <button type="button" disabled={!canPause} onClick={() => runCommand(() => pauseSession(runId))}>
            pause
          </button>
          <button type="button" disabled={!canResume} onClick={() => runCommand(() => resumeSession(runId))}>
            resume
          </button>
          {stopOpen ? (
            <form onSubmit={onStop} className="inline-form">
              <input
                autoFocus
                value={stopSummary}
                onChange={(e) => setStopSummary(e.target.value)}
                placeholder="stop summary (optional)"
              />
              <button type="submit" className="danger">
                confirm stop
              </button>
              <button type="button" onClick={() => setStopOpen(false)}>
                cancel
              </button>
            </form>
          ) : (
            <button type="button" className="danger" disabled={!canStop} onClick={() => setStopOpen(true)}>
              stop…
            </button>
          )}
          {forceOpen ? (
            <form onSubmit={onForceRelease} className="inline-form">
              <input
                autoFocus
                value={forceSummary}
                onChange={(e) => setForceSummary(e.target.value)}
                placeholder="force-release summary (optional)"
              />
              <label className="follow-toggle" title="The stop is appended WITHOUT replaying the stream. Use only when a normal stop cannot work (e.g. a corrupted stream).">
                <input
                  type="checkbox"
                  checked={forceChecked}
                  onChange={(e) => setForceChecked(e.target.checked)}
                />
                bypass replay checks
              </label>
              <button type="submit" className="danger" disabled={!forceChecked}>
                confirm force-release
              </button>
              <button
                type="button"
                onClick={() => {
                  setForceOpen(false);
                  setForceChecked(false);
                }}
              >
                cancel
              </button>
            </form>
          ) : (
            <button
              type="button"
              className="danger"
              disabled={!canForceRelease}
              title="Zombie escape hatch: durably stops the run WITHOUT replaying its stream, releasing the repository claim. The operator is the liveness check — use only when stop cannot work."
              onClick={() => setForceOpen(true)}
            >
              force release…
            </button>
          )}
          <form onSubmit={onInstruct} className="inline-form instruction-form">
            <input
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="send operator instruction…"
              disabled={!canInstruct}
            />
            <button type="submit" disabled={!canInstruct || !instruction.trim()}>
              send
            </button>
          </form>
          <button
            type="button"
            disabled={!canInstruct}
            onClick={() => {
              setAmendText(detail?.objective ?? "");
              setAmendOpen(true);
            }}
          >
            amend objective…
          </button>
          {canFollowUp && (
            <button
              type="button"
              title="Create a new session on the same repository, seeded with a consolidated report from this finished run. You review the objective and press start — nothing auto-chains."
              onClick={onFollowUp}
            >
              follow up →
            </button>
          )}
        </div>
        {amendOpen && detail !== null && (
          <form onSubmit={onAmend} className="amend-panel">
            <p className="muted">
              This durably <strong>replaces</strong> the run&apos;s objective (an{" "}
              <span className="mono">OperatorInstruction</span> with{" "}
              <span className="mono">amends_objective=true</span>) and pauses the driver. Budgets,
              permissions, and sandbox boundaries are unaffected.
            </p>
            <label className="field">
              <span>current objective</span>
              <p className="amend-current">{detail.objective}</p>
            </label>
            <label className="field">
              <span>new objective</span>
              <textarea
                autoFocus
                rows={3}
                value={amendText}
                onChange={(e) => setAmendText(e.target.value)}
              />
            </label>
            <div className="button-row">
              <button type="submit" className="danger" disabled={!amendText.trim()}>
                confirm amendment
              </button>
              <button type="button" onClick={() => setAmendOpen(false)}>
                cancel
              </button>
            </div>
          </form>
        )}
      </section>

      <div className="columns">
        <section className="panel panel-grow">
          <div className="panel-header">
            <h2>event stream ({events.length})</h2>
            <label className="follow-toggle">
              <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
              follow
            </label>
          </div>
          <div className="event-stream" ref={streamRef}>
            {events.length === 0 ? (
              <p className="muted">no events yet</p>
            ) : (
              events.map((envelope) => <EventRow key={envelope.event.event_id} envelope={envelope} />)
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>artifacts ({artifacts.length})</h2>
          </div>
          {artifacts.length === 0 ? (
            <p className="muted">no artifacts recorded</p>
          ) : (
            artifacts.map((artifact) =>
              artifact.kind === "workspace_snapshot" ? (
                <details key={artifact.sequence} className="artifact">
                  <summary>
                    <span className="badge badge-muted">{artifact.kind}</span> {artifact.label}{" "}
                    <span className="muted mono">{formatTime(artifact.occurred_at)}</span>
                  </summary>
                  <DiffViewer
                    content={artifact.content}
                    rollbackDisabled={detail?.driving !== false}
                    onRollbackPaths={(paths) => runCommand(() => rollbackRun(runId, paths))}
                    onRollbackAll={() => runCommand(() => rollbackRun(runId))}
                  />
                </details>
              ) : (
                <details key={artifact.sequence} className="artifact">
                  <summary>
                    <span className="badge badge-muted">{artifact.kind}</span> {artifact.label}{" "}
                    <span className="muted mono">{formatTime(artifact.occurred_at)}</span>
                  </summary>
                  <pre className="artifact-content">{artifact.content}</pre>
                </details>
              ),
            )
          )}
        </section>
      </div>
    </div>
  );
}
