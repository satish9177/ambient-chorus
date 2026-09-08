import styles from "./CaseStateStepper.module.css";

const STATE_ORDER = [
  "CANDIDATE",
  "AWAITING_MANDATES",
  "INVESTIGATING",
  "READY_FOR_ACTION",
  "ACTION_PROPOSED",
  "ACTIONED",
  "VERIFYING",
  "RESOLVED",
  "CLOSED_UNRESOLVED",
] as const;

const LABELS: Record<(typeof STATE_ORDER)[number], string> = {
  CANDIDATE: "Candidate",
  AWAITING_MANDATES: "Awaiting mandates",
  INVESTIGATING: "Investigating",
  READY_FOR_ACTION: "Ready for action",
  ACTION_PROPOSED: "Action proposed",
  ACTIONED: "Actioned",
  VERIFYING: "Verifying",
  RESOLVED: "Resolved",
  CLOSED_UNRESOLVED: "Closed unresolved",
};

/**
 * Renders only the current position among the frozen `CaseState` enum
 * (11-frontend-and-demo.md § Case state stepper). Recomputed from the live state on every
 * render, so a case that returns to `READY_FOR_ACTION` after a missed commitment correctly
 * stops showing the later steps as done — this is a position indicator, not a progress bar
 * that could imply `ACTIONED == RESOLVED`.
 */
export function CaseStateStepper({
  state,
  reasonCode,
}: {
  state: string;
  reasonCode?: string | undefined;
}) {
  const currentIndex = STATE_ORDER.indexOf(state as (typeof STATE_ORDER)[number]);

  return (
    <div>
      <ol className={styles.stepper} aria-label="Case state">
        {STATE_ORDER.map((step, index) => (
          <li
            key={step}
            className={styles.step}
            data-done={currentIndex >= 0 && index < currentIndex}
            data-current={step === state}
            data-terminal={step === "RESOLVED" || step === "CLOSED_UNRESOLVED"}
            aria-current={step === state ? "step" : undefined}
          >
            {LABELS[step]}
          </li>
        ))}
      </ol>
      {reasonCode && <p className={styles.reason}>Reason: {reasonCode}</p>}
    </div>
  );
}
