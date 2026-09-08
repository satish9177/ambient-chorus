import type { DemoActor } from "../../api/session";
import { RESIDENT_ACTORS } from "../../api/session";
import type { CommitmentSafe } from "../../api/types";
import { useVerifyCommitmentMutation } from "../../hooks/useCommitment";
import { Badge } from "../shared/Badge";
import { ErrorBanner } from "../shared/ErrorBanner";
import styles from "./VerificationPanel.module.css";

/**
 * Verification controls only render for a `DUE` commitment, and only submit as whichever
 * resident persona is currently active — the server, not this component, decides whether that
 * resident actually owns the affected fact (08-api-design.md § External reply and
 * verification).
 */
export function VerificationPanel({
  caseId,
  commitment,
  actor,
}: {
  caseId: string;
  commitment: CommitmentSafe;
  actor: DemoActor;
}) {
  const mutation = useVerifyCommitmentMutation(caseId, commitment.commitment_id, actor);
  const isResident = RESIDENT_ACTORS.includes(actor);

  if (commitment.status !== "DUE") return null;

  return (
    <div className={styles.panel} aria-label="Verify commitment">
      <Badge tone="private">Private</Badge>
      <p>
        <strong>{commitment.obligor}</strong> was due to {commitment.action_text}. Did this
        happen?
      </p>
      {!isResident && (
        <p>Switch to the affected resident&apos;s persona to record the outcome.</p>
      )}
      {isResident && (
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            data-variant="fulfilled"
            disabled={mutation.isPending}
            onClick={() =>
              mutation.mutate({ expected_version: commitment.version, outcome: "FULFILLED" })
            }
          >
            Fulfilled
          </button>
          <button
            type="button"
            className={styles.button}
            data-variant="missed"
            disabled={mutation.isPending}
            onClick={() =>
              mutation.mutate({ expected_version: commitment.version, outcome: "MISSED" })
            }
          >
            Missed
          </button>
        </div>
      )}
      {mutation.isError && <ErrorBanner error={mutation.error} />}
    </div>
  );
}
