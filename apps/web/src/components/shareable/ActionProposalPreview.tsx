import type { SafeCurrentAction, SafeDestination } from "../../api/shareable";
import { Badge } from "../shared/Badge";
import styles from "./ActionProposalPreview.module.css";

/**
 * The immutable action preview. There is no edit box: the preview text is the deterministic
 * renderer's own output, and a different wording is a new proposal, never an in-place change
 * (11-frontend-and-demo.md § 3, and 08-api-design.md's proposal/preview immutability).
 */
export function ActionProposalPreview({
  action,
  destination,
}: {
  action: SafeCurrentAction;
  destination: SafeDestination | null;
}) {
  const stale = !action.preview.matches_committed_hash;

  return (
    <div className={styles.panel} aria-labelledby="action-preview-heading">
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <Badge tone="shareable">Shareable</Badge>
        <h3 id="action-preview-heading" style={{ margin: 0 }}>
          Action proposal preview
        </h3>
        <Badge tone="neutral">{action.status}</Badge>
      </div>

      {stale && (
        <p className={styles.staleBanner} role="alert">
          This preview no longer matches the proposal that was committed. It cannot be approved
          — reject it and generate a fresh proposal.
        </p>
      )}

      <p className={styles.destination}>
        To {destination?.display_label ?? "the compiled destination"} · tone {action.tone}
      </p>
      <p className={styles.subject}>{action.subject}</p>

      <div className={styles.preview}>{action.preview.text_body}</div>

      <h4>Safe claims</h4>
      <ul className={styles.claims}>
        {action.claims.map((claim, index) => (
          <li key={index}>
            {claim.text}
            <div className={styles.chips}>
              {claim.export_fact_ids.map((id) => (
                <span key={id} className={styles.chip}>
                  {id}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>

      {action.caveats.length > 0 && (
        <div className={styles.caveats}>
          <strong>Caveats:</strong>{" "}
          {action.caveats.map((caveat) => caveat.text).join(" ")}
        </div>
      )}

      <div className={styles.bindings}>
        <span>view {action.view_hash.slice(0, 16)}…</span>
        <span>proposal {action.proposal_hash.slice(0, 16)}…</span>
        <span>preview {action.preview.preview_hash.slice(0, 16)}…</span>
      </div>
    </div>
  );
}
