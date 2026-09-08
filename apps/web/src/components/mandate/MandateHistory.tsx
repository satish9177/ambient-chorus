import type { MandateVersion } from "../../api/types";
import { Badge } from "../shared/Badge";
import styles from "./MandateHistory.module.css";

export function MandateHistory({ history }: { history: MandateVersion[] }) {
  if (history.length === 0) return null;
  return (
    <section aria-labelledby="mandate-history-heading">
      <h3 id="mandate-history-heading">Decision history</h3>
      <ol className={styles.list}>
        {history.map((version) => (
          <li className={styles.item} key={version.version}>
            <span>
              v{version.version} <Badge tone="private">{version.status}</Badge>
            </span>
            <span className={styles.hash}>{version.terms_hash.slice(0, 20)}…</span>
            <span>{version.decided_at ?? version.revoked_at ?? "pending"}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
