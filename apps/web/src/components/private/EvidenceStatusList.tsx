import type { PrivateFact } from "../../api/private";
import styles from "./PrivateInvestigationPanel.module.css";

export function EvidenceStatusList({ facts }: { facts: PrivateFact[] }) {
  if (facts.length === 0) return <p className={styles.empty}>No facts yet.</p>;
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {facts.map((fact) => (
        <li key={fact.fact_id} className={styles.factRow}>
          <div>{fact.value_preview}</div>
          <div className={styles.factMeta}>
            <span>{fact.fact_type}</span>
            <span>sensitivity: {fact.sensitivity}</span>
            <span>evidence: {fact.evidence_status}</span>
            <span>status: {fact.status}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
