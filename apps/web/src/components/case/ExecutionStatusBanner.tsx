import { useEffect, useRef, useState } from "react";

import { isConflictOperationFailure } from "../../api/client";
import type { SafeExecution } from "../../api/shareable";
import type { DemoActor } from "../../api/session";
import { useStartExecutionMutation } from "../../hooks/useActionFlow";
import { useCaseInvalidation } from "../../hooks/useCase";
import { ErrorBanner } from "../shared/ErrorBanner";
import { OperationProgress } from "../shared/OperationProgress";
import styles from "./ExecutionStatusBanner.module.css";

/**
 * DRAFT / APPROVED / SENDING / SENT / FAILED / SEND_UNKNOWN (11-frontend-and-demo.md § 15).
 * `SEND_UNKNOWN` removes every send/retry control and explains why — there is no retry route,
 * by design (08-api-design.md § Propose, approve, execute).
 *
 * `execution.approval_id` (P2-4) is the durable binding the row itself carries from `APPROVED`
 * onward — read straight from the case surface, not held in component state — so "Execute"
 * survives a page reload between approving and sending without depending on the one-time
 * approval response still being in memory.
 *
 * `approvalAuthorizationCurrent` is the server-derived answer to "is that durable approval
 * still valid for the case's *current* authorization epoch" (P2). When it is `false` — a
 * mandate was revoked, the epoch bumped — "Execute" is withdrawn even though `approval_id` is
 * still readable, and the human is told a fresh compile/proposal/approval path is required.
 *
 * `caseRefetching` is the case surface's own `isFetching` signal, threaded down so "Execute"
 * cannot be clicked while the projection it would submit against is being refreshed — most
 * importantly during the refetch a terminal async conflict triggers, where a second click
 * would otherwise resubmit the now-stale `execution.version` (P2).
 */
export function ExecutionStatusBanner({
  caseId,
  actionId,
  execution,
  approvalAuthorizationCurrent,
  caseRefetching = false,
  isApprover,
  actor,
}: {
  caseId: string;
  actionId: string;
  execution: SafeExecution;
  approvalAuthorizationCurrent: boolean;
  caseRefetching?: boolean;
  isApprover: boolean;
  actor: DemoActor;
}) {
  const mutation = useStartExecutionMutation(caseId, actionId, actor);
  const invalidateCase = useCaseInvalidation(caseId);
  const [operationId, setOperationId] = useState<string | null>(null);
  // Bridges the render gap between an async operation failing with a conflict and TanStack
  // Query flipping `caseRefetching` true: it is set synchronously in the failure handler and
  // held until the refetch it kicked off has actually run and settled, so "Execute" is never
  // clickable in between.
  const [recoveringConflict, setRecoveringConflict] = useState(false);
  const sawRecoveryFetchRef = useRef(false);

  useEffect(() => {
    if (!recoveringConflict) return;
    if (caseRefetching) {
      sawRecoveryFetchRef.current = true;
    } else if (sawRecoveryFetchRef.current) {
      sawRecoveryFetchRef.current = false;
      setRecoveringConflict(false);
    }
  }, [recoveringConflict, caseRefetching]);

  const recovering = recoveringConflict || caseRefetching;

  function send() {
    if (!execution.approval_id || mutation.isPending || recovering) return;
    mutation.mutate(
      {
        execution_id: execution.execution_id,
        expected_execution_version: execution.version,
        approval_id: execution.approval_id,
      },
      { onSuccess: (result) => setOperationId(result.operation_id) },
    );
  }

  // The async counterpart to a synchronous stale-conflict error: the send was accepted (202)
  // but the operation later failed because the execution intent went stale (the replay table
  // refused it, the authorization epoch moved). Discard the operation binding and refetch the
  // case so the next render is built from the server's current state — never re-execute
  // automatically. An ordinary send failure (SES declined, internal error) is left to render
  // as a plain failure by `OperationProgress` and the refetched `execution.state`.
  function handleOperationFailed(errorCode: string | null) {
    if (isConflictOperationFailure(errorCode)) {
      setOperationId(null);
      mutation.reset();
      // Disable "Execute" now, before the refetch below has even started, and keep it disabled
      // until fresh server state has arrived — a second click here would resubmit the stale
      // `execution.version` under a new idempotency key. No automatic retry.
      setRecoveringConflict(true);
    }
    invalidateCase();
  }

  const approvalNoLongerAuthorized =
    execution.state === "APPROVED" &&
    execution.approval_id !== null &&
    !approvalAuthorizationCurrent;

  if (approvalNoLongerAuthorized) {
    return (
      <div className={styles.banner} data-state="STALE_AUTHORIZATION" role="alert">
        <span className={styles.warning}>
          Approval is no longer valid because disclosure authorization changed.
        </span>
        <span>
          A mandate decision moved this case&rsquo;s authorization since it was approved. Compile
          a fresh view, propose again, and obtain a new approval before it can be sent.
        </span>
      </div>
    );
  }

  if (execution.state === "SEND_UNKNOWN") {
    return (
      <div className={styles.banner} data-state="SEND_UNKNOWN" role="alert">
        <span className={styles.warning}>Delivery outcome is unknown.</span>
        <span>
          The send may or may not have reached the destination. To prevent a duplicate message,
          there is no retry — this requires manual reconciliation outside the demo.
        </span>
      </div>
    );
  }

  if (execution.state === "SENT") {
    return (
      <div className={styles.banner} data-state="SENT" role="status">
        <span>Sent.</span>
        <span>This means the message went out — it does not mean the case is resolved.</span>
      </div>
    );
  }

  if (execution.state === "FAILED") {
    return (
      <div className={styles.banner} data-state="FAILED" role="alert">
        <span>Send failed.</span>
        <span>Propose a fresh action to try again — there is no retry on this execution.</span>
      </div>
    );
  }

  if (execution.state === "APPROVED") {
    return (
      <div className={styles.banner} data-state="APPROVED">
        {isApprover && execution.approval_id ? (
          <button
            type="button"
            className={styles.button}
            onClick={send}
            disabled={mutation.isPending || recovering}
          >
            {mutation.isPending ? "Sending…" : "Execute / send"}
          </button>
        ) : (
          <span>Approved. {!isApprover && "Switch to the case approver persona to send it."}</span>
        )}
        {mutation.isError && <ErrorBanner error={mutation.error} />}
        <OperationProgress
          operationId={operationId}
          actor={actor}
          label="Send"
          onSucceeded={invalidateCase}
          onFailed={handleOperationFailed}
        />
      </div>
    );
  }

  return null;
}
