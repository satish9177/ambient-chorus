import type { Contradiction } from "../../api/private";
import styles from "./PrivateInvestigationPanel.module.css";

export function ContradictionCard({ contradiction }: { contradiction: Contradiction }) {
  return (
    <div className={styles.contradiction} role="note">
      <strong>{contradiction.materiality} contradiction:</strong> {contradiction.description}
    </div>
  );
}
