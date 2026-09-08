import type { SafeCurrentAction } from "../../api/shareable";
import { useApproveActionMutation } from "../../hooks/useActionFlow";
import type { DemoActor } from "../../api/session";
import { ErrorBanner } from "../shared/ErrorBanner";
import styles from "./ApprovalPanel.module.css";

/**
 * Records exactly one human decision using the server's own hashes and version — never a
 * value the browser derives or guesses (08-api-design.md § Propose, approve, execute).
 */
export function ApprovalPanel({
  caseId,
  action,
  actor,
  isApprover,
  caseRefetching = false,
}: {
  caseId: string;
  action: SafeCurrentAction;
  actor: DemoActor;
  isApprover: boolean;
  /**
   * True while the case surface is being refetched — after a stale/binding conflict,
   * `handleMutationError` invalidates it. The decision controls stay disabled until the fresh
   * state lands, so the next click is always made against current hashes and versions (P2).
   */
  caseRefetching?: boolean;
}) {
  const mutation = useApproveActionMutation(caseId, action.action_id, actor);
  const stale = !action.preview.matches_committed_hash;
  const canDecide =
    isApprover && action.execution.state === "DRAFT" && !stale && !caseRefetching;

  // The mutation's own `onSuccess` already invalidates the case query, and the refetched
  // surface carries `execution.approval_id` (P2-4) — nothing needs to be threaded through a
  // callback here for `ExecutionStatusBanner` to find its binding, including after a reload.
  function decide(decision: "APPROVED" | "REJECTED") {
    mutation.mutate({
      decision,
      expected_execution_version: action.execution.version,
      execution_id: action.execution.execution_id,
      view_hash: action.view_hash,
      proposal_hash: action.proposal_hash,
      preview_hash: action.preview.preview_hash,
    });
  }

  if (!isApprover) {
    return (
      <div className={styles.panel}>
        <p>Switch to the case approver persona to approve or reject this proposal.</p>
      </div>
    );
  }

  if (action.execution.state !== "DRAFT") {
    return null;
  }

  return (
    <div className={styles.panel} aria-label="Approval decision">
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          data-variant="approve"
          onClick={() => decide("APPROVED")}
          disabled={!canDecide || mutation.isPending}
        >
          {mutation.isPending ? "Approving…" : "Approve"}
        </button>
        <button
          type="button"
          className={styles.button}
          data-variant="reject"
          onClick={() => decide("REJECTED")}
          disabled={mutation.isPending || caseRefetching}
        >
          Reject
        </button>
      </div>
      {mutation.isError && <ErrorBanner error={mutation.error} />}
    </div>
  );
}
