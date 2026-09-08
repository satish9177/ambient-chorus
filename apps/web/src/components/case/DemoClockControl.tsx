import { useState } from "react";

import type { DemoActor } from "../../api/session";
import type { CommitmentSafe } from "../../api/types";
import { useAdvanceClockMutation } from "../../hooks/useCommitment";
import { ErrorBanner } from "../shared/ErrorBanner";
import styles from "./ExternalReplyControl.module.css";

const BUFFER_SECONDS = 3600;
const MAX_ADVANCE_SECONDS = 60 * 24 * 3600;

/**
 * A demo-only control (never a production feature) that advances the logical clock far enough
 * to make one commitment due, then invokes the same watcher a real schedule would
 * (11-frontend-and-demo.md § 17).
 */
export function DemoClockControl({
  caseId,
  commitment,
  logicalNow,
  actor,
  onAdvanced,
}: {
  caseId: string;
  commitment: CommitmentSafe;
  logicalNow: string;
  actor: DemoActor;
  onAdvanced: (logicalNow: string) => void;
}) {
  const mutation = useAdvanceClockMutation(caseId, actor);
  const [lastOutcome, setLastOutcome] = useState<string | null>(null);

  const dueAtMs = new Date(commitment.due_at).getTime();
  const nowMs = new Date(logicalNow).getTime();
  const seconds = Math.min(
    MAX_ADVANCE_SECONDS,
    Math.max(1, Math.ceil((dueAtMs - nowMs) / 1000) + BUFFER_SECONDS),
  );

  return (
    <div className={styles.panel} aria-label="Demo clock">
      <span>Demo clock: {new Date(logicalNow).toLocaleString()}</span>
      <button
        type="button"
        className={styles.button}
        onClick={() =>
          mutation.mutate(
            { case_id: caseId, commitment_id: commitment.commitment_id, advance_seconds: seconds },
            {
              onSuccess: (result) => {
                setLastOutcome(result.watcher_outcome);
                onAdvanced(result.logical_now);
              },
            },
          )
        }
        disabled={mutation.isPending}
      >
        {mutation.isPending ? "Advancing…" : "Advance clock past due date"}
      </button>
      {lastOutcome && <span>Watcher outcome: {lastOutcome}</span>}
      {mutation.isError && <ErrorBanner error={mutation.error} />}
    </div>
  );
}
