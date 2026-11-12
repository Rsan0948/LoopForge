import { useEffect, useState, type ReactElement } from "react";

type Question = { topic: string; deadline_epoch: number; answer: string };
type Report = {
  policy: { objective: string; deadline_epoch: number; clarifications: { topic: string; question: string; classification: string; fallback: string }[] } | null;
  sequence: number; candidate_changed_since_verification: boolean; phase: string; execution_status: string; disposition: string; plan: string; summary: string;
  spent_microusd: number; reserved_microusd: number; remaining_microusd: number;
  paid_spent_microusd: number; paid_reserved_microusd: number;
  runner: { status: string; armed: boolean; error?: string };
  dynamic_questions: { topic: string; question: string; classification: string; fallback: string }[];
  paused: boolean; control_requested: string; late_answer_conflict: boolean;
  questions: Question[]; experiments: { topic: string; branch: string; checkpoint: string; assumption: string }[];
  diff: string; diff_truncated: boolean; accepted_commit: string;
  observation: { verified: boolean; status: string; candidate_sha256: string };
};
type Summary = { id: string; execution_status: string; disposition: string; spent_microusd: number };
const money = (value: number): string => `$${(value / 1000000).toFixed(6)}`;
async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

export default function GoalsView({ goalId }: { goalId?: string }): ReactElement {
  const [goals, setGoals] = useState<Summary[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const refresh = async (): Promise<void> => {
      try {
        if (goalId) {
          const next = await read<Report>(`/api/goals/${encodeURIComponent(goalId)}`, controller.signal);
          if (active) setReport(current => current && current.sequence > next.sequence ? current : next);
        } else {
          const next = await read<Summary[]>("/api/goals", controller.signal);
          if (active) setGoals(next);
        }
      } catch (reason) {
        if (active) setError(String(reason));
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 3000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [goalId]);
  const command = async (action: string, topic?: string): Promise<void> => {
    if (!goalId) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/goals/${encodeURIComponent(goalId)}/commands`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-LoopForge-Operator": "1" },
        body: JSON.stringify({ command: action, topic, answer: topic ? answers[topic] : undefined }),
      });
      if (!response.ok) { const detail = await response.json() as { detail?: string }; throw new Error(detail.detail ?? `Request failed (${response.status})`); }
      const result = await response.json() as { id?: string };
      if (action === "successor" && result.id) { window.location.hash = `#/goals/${encodeURIComponent(result.id)}`; return; }
      setReport(await read<Report>(`/api/goals/${encodeURIComponent(goalId)}`));
    } catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  };
  if (!goalId) return <section><h1>Goals</h1>{error ? <p role="alert">{error}</p> : null}
    {goals.length === 0 ? <p>No goals in the configured goal directory. Create a frozen repository goal through the CLI, then start it here.</p> : <ul>{goals.map(goal => <li key={goal.id}><a href={`#/goals/${encodeURIComponent(goal.id)}`}>{goal.id}</a> — {goal.execution_status}; {goal.disposition}; {money(goal.spent_microusd)}</li>)}</ul>}</section>;
  if (!report) return <section><h1>Goal</h1>{error ? <p role="alert">{error}</p> : <p>Loading goal…</p>}</section>;
  const contract = report.policy;
  if (!contract) return <section><a href="#/goals">← Goals</a><h1>Goal intake incomplete</h1><p>No frozen goal contract has been recorded. Inspect the retained intake or checkpoint before creating a new goal.</p>{error ? <p role="alert">{error}</p> : null}</section>;
  const terminal = ["delivered", "blocked", "cancelled"].includes(report.phase);
  return <section className="goal-review"><a href="#/goals">← Goals</a><h1>Goal morning report</h1>
    <h2>{contract.objective}</h2><p role="status">{report.execution_status} · runner: {report.runner?.status ?? "offline"} · candidate: {report.observation.status} · disposition: {report.disposition}</p>
    {error ? <p role="alert">{error}</p> : null}
    <p>Spent {money(report.paid_spent_microusd ?? report.spent_microusd)} · reserved {money(report.paid_reserved_microusd ?? report.reserved_microusd)} · remaining {money(report.remaining_microusd)}</p>
    <p>Deadline: {new Date(contract.deadline_epoch * 1000).toLocaleString()}</p>
    <p>{report.summary}</p><h2>Current plan</h2><p>{report.plan || "No supervisor decision yet."}</p>
    {report.late_answer_conflict ? <p role="alert">A late answer conflicts with the experiment assumption. Work is retained; continuing and acceptance are blocked. Prepare a linked continuation below; the experiment and its evidence will be retained.</p> : null}
    <div className="goal-controls">
      <button disabled={busy || terminal || report.runner?.status === "running"} onClick={() => { void command("start"); }}>Start runner</button>
      {report.late_answer_conflict ? <button disabled={busy || (!report.paused && !terminal)} onClick={() => { void command("successor"); }}>Prepare linked continuation</button> : null}
      <button disabled={busy || terminal || report.paused || !!report.control_requested} onClick={() => { void command("pause"); }}>Pause</button>
      <button disabled={busy || terminal || !report.paused || report.late_answer_conflict} onClick={() => { void command("resume"); }}>Resume</button>
      <button disabled={busy || terminal || report.control_requested === "cancel"} onClick={() => { void command("cancel"); }}>Cancel</button>
      <button disabled={busy || report.phase !== "delivered" || !report.observation.verified || report.disposition !== "pending" || report.late_answer_conflict || report.candidate_changed_since_verification} onClick={() => { void command("accept"); }}>Accept candidate</button>
      <button disabled={busy || !terminal || report.disposition !== "pending"} onClick={() => { void command("discard"); }}>Discard candidate</button>
    </div><p>Pause and cancel take effect after in-flight work reconciles. Accept commits the isolated candidate branch. Discard retains files for inspection.</p>
    <h2>Clarifications and assumptions</h2>
    {report.questions.length === 0 ? <p>No outstanding clarifications.</p> : report.questions.map(question => {
      const policy = [...contract.clarifications, ...(report.dynamic_questions ?? [])].find(item => item.topic === question.topic);
      return <article key={question.topic}><h3>{policy?.question ?? question.topic}</h3>
        <p>{policy?.classification} · response deadline {new Date(question.deadline_epoch * 1000).toLocaleString()}</p>
        {question.answer ? <p>Answer: {question.answer}</p> : <><label htmlFor={`answer-${question.topic}`}>Your answer</label><textarea id={`answer-${question.topic}`} maxLength={2000} value={answers[question.topic] ?? ""} onChange={event => setAnswers(current => ({ ...current, [question.topic]: event.target.value }))} /><button disabled={busy || !(answers[question.topic] ?? "").trim()} onClick={() => { void command("answer", question.topic); }}>Submit answer</button></>}
      </article>;
    })}
    {report.experiments.map(experiment => <article key={experiment.branch}><h3>Experiment: {experiment.branch}</h3><p>Assumption: {experiment.assumption}</p><p>Checkpoint: <code>{experiment.checkpoint}</code></p></article>)}
    {report.candidate_changed_since_verification ? <p role="alert">Candidate files changed after verification. Acceptance is blocked.</p> : null}
    <h2>Candidate diff</h2><p>Verified: {report.observation.verified ? "yes" : "no"} · identity <code>{report.observation.candidate_sha256 || "not available"}</code></p>
    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{report.diff || "No candidate changes recorded."}</pre>{report.diff_truncated ? <p>Diff truncated. Full changes remain in the local candidate repository.</p> : null}
    {report.accepted_commit ? <p>Accepted commit: <code>{report.accepted_commit}</code></p> : null}
  </section>;
}
