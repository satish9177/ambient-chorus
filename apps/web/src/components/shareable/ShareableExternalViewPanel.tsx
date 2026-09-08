import type { PrivacyCounts, ShareableCaseView } from "../../api/shareable";
import { Badge } from "../shared/Badge";
import styles from "./ShareableExternalViewPanel.module.css";

/**
 * The shareable half of the boundary comparison. This module — and everything under
 * `components/shareable/**` — may import only `api/shareable`, never `api/private`
 * (`eslint.config.js`'s `no-restricted-imports`, and `api/boundary.test.ts`).
 */
export function ShareableExternalViewPanel({
  view,
  privacyCounts,
}: {
  view: ShareableCaseView | null;
  privacyCounts: PrivacyCounts | null;
}) {
  return (
    <div className={styles.panel} aria-labelledby="shareable-view-heading">
      <div className={styles.header}>
        <Badge tone="shareable">Shareable</Badge>
        <h3 id="shareable-view-heading" className={styles.title}>
          Shareable external view
        </h3>
      </div>

      {!view && <p className={styles.empty}>No compiled view yet. Compile from the private panel.</p>}

      {view && (
        <>
          <p className={styles.meta}>
            To {view.destination.display_label} · purpose {view.purpose} · view{" "}
            {view.view_id.slice(0, 8)}…
          </p>

          {privacyCounts && (
            <div className={styles.counts}>
              <span>Included: {privacyCounts.included}</span>
              <span>Excluded: {privacyCounts.excluded}</span>
            </div>
          )}

          {view.shareable_facts.map((fact) => (
            <div key={fact.export_fact_id} className={styles.factRow}>
              <div>{fact.safe_text}</div>
              <div className={styles.factMeta}>
                <span>{fact.fact_type}</span>
                <span>scope: {fact.effective_scope}</span>
                <span>evidence: {fact.evidence_status}</span>
                <span>{fact.contributor_count} contributor(s)</span>
              </div>
            </div>
          ))}

          {view.safe_evidence_refs.length > 0 && (
            <div className={styles.factRow}>
              <strong>Evidence:</strong>{" "}
              {view.safe_evidence_refs.map((ref) => ref.caption).join("; ")}
            </div>
          )}
        </>
      )}
    </div>
  );
}
