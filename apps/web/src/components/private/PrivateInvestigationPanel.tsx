import type { Investigation } from "../../api/private";
import { Badge } from "../shared/Badge";
import { ContradictionCard } from "./ContradictionCard";
import { EvidenceStatusList } from "./EvidenceStatusList";
import styles from "./PrivateInvestigationPanel.module.css";
import { PrivacyDecisionTable } from "./PrivacyDecisionTable";

/**
 * The private half of the boundary comparison. Never imported by anything under
 * `components/shareable/**` — see `eslint.config.js`'s `no-restricted-imports`.
 */
export function PrivateInvestigationPanel({
  investigation,
}: {
  investigation: Investigation | null;
}) {
  return (
    <div className={styles.panel} aria-labelledby="private-investigation-heading">
      <div className={styles.header}>
        <Badge tone="private">Private</Badge>
        <h3 id="private-investigation-heading" className={styles.title}>
          Private investigation
        </h3>
      </div>

      {!investigation && (
        <p className={styles.empty}>Run an investigation to see private findings here.</p>
      )}

      {investigation && (
        <>
          <div className={styles.section}>
            <h4>Reports ({investigation.reports.length})</h4>
          </div>

          <div className={styles.section}>
            <h4>Facts</h4>
            <EvidenceStatusList facts={investigation.facts} />
          </div>

          {investigation.assessment && (
            <>
              <div className={styles.section}>
                <h4>
                  Assessment — {investigation.assessment.independent_source_count} independent
                  source(s), {investigation.assessment.is_corroborated ? "corroborated" : "not corroborated"}
                </h4>
                <p className={styles.empty}>
                  Recommended: {investigation.assessment.recommended_disposition}
                </p>
              </div>

              <div className={styles.section}>
                <h4>Contradictions</h4>
                {investigation.assessment.contradictions.length === 0 && (
                  <p className={styles.empty}>None found.</p>
                )}
                {investigation.assessment.contradictions.map((contradiction, index) => (
                  <ContradictionCard key={index} contradiction={contradiction} />
                ))}
              </div>

              {investigation.assessment.alternative_explanations.length > 0 && (
                <div className={styles.section}>
                  <h4>Alternative explanations</h4>
                  {investigation.assessment.alternative_explanations.map((alt, index) => (
                    <p key={index} className={styles.factRow}>
                      {alt.description}
                    </p>
                  ))}
                </div>
              )}
            </>
          )}

          <div className={styles.section}>
            <h4>Privacy compiler decisions</h4>
            <PrivacyDecisionTable compile={investigation.compile} />
          </div>
        </>
      )}
    </div>
  );
}
