import { Link } from "react-router-dom";

import type { ChorusSignal, FeedItem } from "../../api/types";
import styles from "./CandidateClusterRail.module.css";

function uniqueSignals(items: FeedItem[]): ChorusSignal[] {
  const seen = new Map<string, ChorusSignal>();
  for (const item of items) {
    if (item.chorus_signal) seen.set(item.chorus_signal.candidate_case_id, item.chorus_signal);
  }
  return [...seen.values()];
}

/** The demo's first deliberate reveal: "Potential recurring issue detected." */
export function CandidateClusterRail({ items }: { items: FeedItem[] }) {
  const signals = uniqueSignals(items);
  if (signals.length === 0) return null;

  return (
    <div className={styles.rail}>
      {signals.map((signal) => (
        <div className={styles.card} key={signal.candidate_case_id}>
          <div>
            <p className={styles.title}>Potential recurring issue detected</p>
            <p className={styles.subtitle}>
              {signal.label} — {signal.related_count} linked reports, status {signal.status}
            </p>
          </div>
          <Link className={styles.cta} to={`/cases/${signal.candidate_case_id}`}>
            Open case
          </Link>
        </div>
      ))}
    </div>
  );
}
