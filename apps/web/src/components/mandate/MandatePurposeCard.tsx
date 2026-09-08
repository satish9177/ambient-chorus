import { Badge } from "../shared/Badge";
import styles from "./MandatePurposeCard.module.css";

export function MandatePurposeCard({ status }: { status: string }) {
  return (
    <div className={styles.card}>
      <div className={styles.badgeRow}>
        <Badge tone="private">Private</Badge>
        <span>Mandate status: {status}</span>
      </div>
      <h3 className={styles.title}>These permissions are yours to decide</h3>
      <p className={styles.explainer}>
        These permissions are private and are not automatically shared externally. Nothing
        below travels outside this case until you approve it, and the compiler still applies
        the community&apos;s own policy ceiling on top of whatever you allow.
      </p>
    </div>
  );
}
