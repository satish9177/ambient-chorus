import styles from "./DecisionBar.module.css";

export function DecisionBar({
  canDecide,
  status,
  isAdjusting,
  pending,
  onApprove,
  onStartAdjust,
  onSubmitAdjust,
  onCancelAdjust,
  onRefuse,
  onRevoke,
}: {
  canDecide: boolean;
  status: string;
  isAdjusting: boolean;
  pending: boolean;
  onApprove: () => void;
  onStartAdjust: () => void;
  onSubmitAdjust: () => void;
  onCancelAdjust: () => void;
  onRefuse: () => void;
  onRevoke: () => void;
}) {
  if (!canDecide) {
    return (
      <p className={styles.hint}>
        Switch to this resident&apos;s persona to approve, adjust, or refuse this mandate.
      </p>
    );
  }

  const decided = status === "APPROVED" || status === "ADJUSTED";

  if (isAdjusting) {
    return (
      <div className={styles.bar} role="group" aria-label="Adjust mandate">
        <button
          type="button"
          className={styles.button}
          data-variant="adjust"
          onClick={onSubmitAdjust}
          disabled={pending}
        >
          {pending ? "Submitting…" : "Submit adjustment"}
        </button>
        <button type="button" className={styles.button} onClick={onCancelAdjust} disabled={pending}>
          Cancel
        </button>
      </div>
    );
  }

  if (status !== "PROPOSED" && !decided) {
    return <p className={styles.hint}>This mandate is {status.toLowerCase()}.</p>;
  }

  return (
    <div className={styles.bar} role="group" aria-label="Mandate decision">
      {!decided && (
        <>
          <button
            type="button"
            className={styles.button}
            data-variant="approve"
            onClick={onApprove}
            disabled={pending}
          >
            Approve
          </button>
          <button
            type="button"
            className={styles.button}
            data-variant="adjust"
            onClick={onStartAdjust}
            disabled={pending}
          >
            Adjust
          </button>
          <button
            type="button"
            className={styles.button}
            data-variant="refuse"
            onClick={onRefuse}
            disabled={pending}
          >
            Refuse
          </button>
        </>
      )}
      {decided && (
        <button
          type="button"
          className={styles.button}
          data-variant="revoke"
          onClick={onRevoke}
          disabled={pending}
        >
          Revoke
        </button>
      )}
    </div>
  );
}
