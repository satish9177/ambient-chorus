import { Link } from "react-router-dom";

import type { EvidenceSummaryRow } from "../../api/types";

/**
 * Real in-app links to every contributor's mandate thread for this case, derived from the
 * case surface's own `evidence_summary` (each row already names its owning `contributor_id`)
 * rather than a URL a person or a test has to construct (P2-3).
 */
export function MandateContributorLinks({
  caseId,
  rows,
}: {
  caseId: string;
  rows: EvidenceSummaryRow[];
}) {
  const contributorIds = [...new Set(rows.map((row) => row.contributor_id))];
  if (contributorIds.length === 0) return null;

  return (
    <nav aria-label="Contributor mandate threads">
      <ul>
        {contributorIds.map((contributorId) => (
          <li key={contributorId}>
            <Link to={`/mandates/${contributorId}?case=${caseId}`}>
              Mandate thread — {contributorId.slice(0, 8)}…
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
