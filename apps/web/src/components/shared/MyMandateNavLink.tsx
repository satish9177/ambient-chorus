import { NavLink } from "react-router-dom";

import { RESIDENT_ACTORS } from "../../api/session";
import { useDemoCase } from "../../context/DemoCaseContext";
import { usePersona } from "../../context/PersonaContext";
import { useSessionQuery } from "../../hooks/useSession";

/**
 * A real in-app link to the active resident's own mandate thread, resolved from `GET
 * /session` rather than a URL anyone had to construct by hand (P2-3). It only ever appears for
 * a resident persona, and only once a case id has become known to this browser session (a
 * resident cannot discover one themselves — the feed and case surface are presenter/approver
 * reads — so there is nothing to link to until the presenter has opened one).
 */
export function MyMandateNavLink() {
  const { actor } = usePersona();
  const { caseId } = useDemoCase();
  const sessionQuery = useSessionQuery(actor);

  if (!RESIDENT_ACTORS.includes(actor) || !caseId) return null;
  const contributorId = sessionQuery.data?.contributor_id;
  if (!contributorId) return null;

  return (
    <NavLink to={`/mandates/${contributorId}?case=${caseId}`}>My mandate</NavLink>
  );
}
