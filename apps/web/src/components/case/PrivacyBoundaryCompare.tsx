import type { ReactNode } from "react";

import styles from "./PrivacyBoundaryCompare.module.css";

/**
 * The visual centerpiece: private and shareable side by side on desktop, stacked with
 * persistent labels on narrow screens (11-frontend-and-demo.md § 3). A pure layout — the
 * private/shareable type boundary lives in which components the caller passes as `left`/
 * `right`, not in this one.
 *
 * P3-11: the relationship is stated once, prominently, as real heading text — never
 * `aria-hidden` — so it reads clearly at recording resolution and to a screen reader alike.
 * The two panels underneath still carry their own persistent PRIVATE/SHAREABLE badges
 * (`PrivateInvestigationPanel`/`ShareableExternalViewPanel`), so the labels survive even if
 * this banner scrolls out of view.
 */
export function PrivacyBoundaryCompare({ left, right }: { left: ReactNode; right: ReactNode }) {
  return (
    <section aria-labelledby="privacy-boundary-heading">
      <div className={styles.banner}>
        <h3 id="privacy-boundary-heading">
          <span className={styles.privateChip}>Private</span>
          <span className={styles.arrow} aria-hidden="true">
            →
          </span>
          <span className={styles.compilerLabel}>Deterministic privacy compiler</span>
          <span className={styles.arrow} aria-hidden="true">
            →
          </span>
          <span className={styles.shareableChip}>Shareable</span>
        </h3>
      </div>
      <div className={styles.compare}>
        <div>{left}</div>
        <div className={styles.divider} aria-hidden="true">
          →
        </div>
        <div>{right}</div>
      </div>
    </section>
  );
}
