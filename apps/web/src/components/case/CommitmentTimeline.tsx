import type { CommitmentSafe } from "../../api/types";
import { Badge, type BadgeTone } from "../shared/Badge";
import styles from "./CommitmentTimeline.module.css";

const STATUS_TONE: Record<string, BadgeTone> = {
  PENDING: "neutral",
  DUE: "private",
  FULFILLED: "success",
  MISSED: "danger",
  CANCELLED: "neutral",
};

/**
 * The safe half of `CommitmentScheduleProjection` (P2-9) — never `scheduler_name`, a due-event
 * id, or any other transport/replay identity, only the two-value status and a closed failure
 * code, exactly what `apps/api/chorus_api/routes/cases.py`'s `CommitmentSafeResponse` exposes.
 */
function scheduleLabel(commitment: CommitmentSafe): { text: string; tone: BadgeTone } | null {
  switch (commitment.schedule_status) {
    case "CREATED":
      return { text: "Scheduled", tone: "success" };
    case "PENDING_SCHEDULE":
      return commitment.schedule_last_error_code
        ? { text: `Scheduling failed — retrying (${commitment.schedule_last_error_code})`, tone: "danger" }
        : { text: "Pending scheduling", tone: "neutral" };
    default:
      return null;
  }
}

export function CommitmentTimeline({ commitments }: { commitments: CommitmentSafe[] }) {
  if (commitments.length === 0) {
    return <p>No commitments yet — one appears once a management reply is delivered.</p>;
  }

  return (
    <ul className={styles.list} aria-label="Commitments">
      {commitments.map((commitment) => {
        const schedule = scheduleLabel(commitment);
        return (
          <li key={commitment.commitment_id} className={styles.item}>
            <div>
              <div className={styles.text}>
                {commitment.obligor}: {commitment.action_text}
              </div>
              <div className={styles.meta}>
                Due {new Date(commitment.due_at).toLocaleString()} · verified by{" "}
                {commitment.verification_method}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {schedule && <Badge tone={schedule.tone}>{schedule.text}</Badge>}
              <Badge tone={STATUS_TONE[commitment.status] ?? "neutral"}>{commitment.status}</Badge>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
