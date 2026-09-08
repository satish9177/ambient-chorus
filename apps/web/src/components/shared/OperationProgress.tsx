import { useEffect, useRef } from "react";

import { useOperationPoll } from "../../hooks/useOperationPoll";
import type { DemoActor } from "../../api/session";
import { ErrorBanner } from "./ErrorBanner";
import styles from "./OperationProgress.module.css";

export function OperationProgress({
  operationId,
  actor,
  label,
  onSucceeded,
  onFailed,
}: {
  operationId: string | null;
  actor: DemoActor;
  label: string;
  onSucceeded?: () => void;
  /**
   * Fired once when the operation reaches a terminal `FAILED` status, with the operation's own
   * `error_code` (or `null`). This is the async counterpart to a synchronous mutation error:
   * `POST` was accepted (202) but the operation later failed, and the caller may need to
   * discard a now-stale intent and refetch — polling stopping is not enough (P2).
   */
  onFailed?: (errorCode: string | null) => void;
}) {
  const poll = useOperationPoll(operationId, actor);
  const notifiedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!operationId || notifiedRef.current === operationId) return;
    if (poll.data?.status === "SUCCEEDED") {
      notifiedRef.current = operationId;
      onSucceeded?.();
    } else if (poll.data?.status === "FAILED") {
      notifiedRef.current = operationId;
      onFailed?.(poll.data.error_code ?? null);
    }
  }, [poll.data?.status, poll.data?.error_code, operationId, onSucceeded, onFailed]);

  if (!operationId) return null;

  if (poll.isError) {
    return <ErrorBanner error={poll.error} />;
  }

  if (poll.timedOut) {
    return (
      <div className={styles.wrap} data-status="TIMED_OUT" role="status">
        <span>
          {label} is taking longer than expected. The operation is still running on the server —
          refresh to check on it.
        </span>
      </div>
    );
  }

  const status = poll.data?.status ?? "PENDING";

  if (status === "SUCCEEDED") {
    return (
      <div className={styles.wrap} data-status="SUCCEEDED" role="status">
        <span>{label}: done.</span>
      </div>
    );
  }

  if (status === "FAILED") {
    return (
      <div className={styles.wrap} data-status="FAILED" role="alert">
        <span>{label} failed{poll.data?.error_code ? ` (${poll.data.error_code})` : ""}.</span>
      </div>
    );
  }

  return (
    <div className={styles.wrap} data-status={status} role="status" aria-live="polite">
      <span className={styles.spinner} aria-hidden="true" />
      <span>
        {label}: {status.toLowerCase()}…
      </span>
    </div>
  );
}
