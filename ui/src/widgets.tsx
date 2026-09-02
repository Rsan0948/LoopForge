import type { ReactElement } from "react";
import type { RunStatus } from "./api";

const STATUS_CLASS: Record<RunStatus, string> = {
  created: "badge badge-muted",
  planning: "badge badge-active",
  ready: "badge badge-active",
  acting: "badge badge-active",
  verifying: "badge badge-active",
  reflecting: "badge badge-active",
  waiting_for_approval: "badge badge-amber",
  succeeded: "badge badge-green",
  failed: "badge badge-red",
  stalled: "badge badge-red",
  budget_exhausted: "badge badge-red",
  cancelled: "badge badge-muted",
};

export function StatusBadge({ status }: { status: RunStatus }): ReactElement {
  return <span className={STATUS_CLASS[status] ?? "badge"}>{status}</span>;
}

export function ErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }): ReactElement {
  return (
    <div className="error-banner" role="alert">
      <span>{message}</span>
      {onDismiss !== undefined && (
        <button type="button" className="link-button" onClick={onDismiss}>
          dismiss
        </button>
      )}
    </div>
  );
}
