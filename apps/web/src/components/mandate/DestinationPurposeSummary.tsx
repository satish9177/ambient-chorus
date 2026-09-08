import styles from "./FactPermissionRow.module.css";

export function DestinationPurposeSummary({
  destinations,
  purposes,
  expiresAt,
}: {
  destinations: string[];
  purposes: string[];
  expiresAt: string | null;
}) {
  return (
    <div className={styles.row} style={{ gridTemplateColumns: "1fr" }}>
      <div>
        <p className={styles.wording}>Where this can go, and why</p>
        <div className={styles.meta}>
          <span>Destination: {destinations.join(", ") || "none"}</span>
          <span>Purpose: {purposes.join(", ") || "none"}</span>
          <span>Expires: {expiresAt ?? "no expiry set"}</span>
        </div>
      </div>
    </div>
  );
}
