import type { PrivateCompileExplanation } from "../../api/private";
import styles from "./PrivateInvestigationPanel.module.css";

/**
 * The private per-fact compile decision table. `ExcludedFactBody` pairs a private fact ID with
 * its denial reason, which is exactly the pairing that must never reach the shareable zone
 * (08-api-design.md § Case surfaces) — so this table only ever renders inside
 * `PrivateInvestigationPanel`.
 */
export function PrivacyDecisionTable({ compile }: { compile: PrivateCompileExplanation | null }) {
  if (!compile) return <p className={styles.empty}>No compile has run yet.</p>;

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" }}>
      <caption style={{ textAlign: "left", marginBottom: 4, color: "var(--color-private-text)" }}>
        Compile {compile.compile_id.slice(0, 8)}… — decision {compile.decision}
      </caption>
      <thead>
        <tr>
          <th style={{ textAlign: "left" }}>Fact</th>
          <th style={{ textAlign: "left" }}>Included</th>
          <th style={{ textAlign: "left" }}>Scope</th>
          <th style={{ textAlign: "left" }}>Reason codes</th>
        </tr>
      </thead>
      <tbody>
        {compile.facts.map((fact) => (
          <tr key={fact.fact_id}>
            <td>{fact.fact_id.slice(0, 8)}…</td>
            <td>{fact.included ? "yes" : "no"}</td>
            <td>{fact.granted_scope ?? "—"}</td>
            <td>{fact.reason_codes.join(", ") || "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
