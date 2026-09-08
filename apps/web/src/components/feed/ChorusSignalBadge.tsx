import { Link } from "react-router-dom";

import type { ChorusSignal } from "../../api/types";
import { Badge } from "../shared/Badge";
import styles from "./ChorusSignalBadge.module.css";

export function ChorusSignalBadge({ signal }: { signal: ChorusSignal }) {
  return (
    <Link className={styles.link} to={`/cases/${signal.candidate_case_id}`}>
      <Badge tone="shareable">Chorus signal</Badge>
      <span className={styles.label}>
        {signal.label} · {signal.related_count} linked reports
      </span>
    </Link>
  );
}
