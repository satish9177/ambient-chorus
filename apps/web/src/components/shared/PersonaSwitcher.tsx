import type { CSSProperties } from "react";

import { ACTOR_LABELS, DEMO_ACTORS, RESIDENT_ACTORS, type DemoActor } from "../../api/session";
import { usePersona } from "../../context/PersonaContext";
import { useSessionQuery } from "../../hooks/useSession";
import styles from "./PersonaSwitcher.module.css";

function roleOf(actor: DemoActor): "presenter" | "resident" | "approver" {
  if (actor === "presenter_admin") return "presenter";
  if (actor === "case_approver") return "approver";
  return "resident";
}

/**
 * The demo persona switcher (11-frontend-and-demo.md § 6). Switching re-reads `GET /session`
 * for the new actor rather than assuming anything client-side about capabilities or identity.
 */
export function PersonaSwitcher() {
  const { actor, setActor } = usePersona();
  const sessionQuery = useSessionQuery(actor);
  const role = roleOf(actor);

  return (
    <div className={styles.wrap} data-testid="persona-switcher">
      <span className={styles.dot} data-role={role} aria-hidden="true" />
      <label htmlFor="persona-select" style={visuallyHidden}>
        Active demo persona
      </label>
      <select
        id="persona-select"
        className={styles.select}
        value={actor}
        onChange={(event) => setActor(event.target.value as DemoActor)}
      >
        {DEMO_ACTORS.map((option) => (
          <option key={option} value={option}>
            {ACTOR_LABELS[option]}
          </option>
        ))}
      </select>
      <span className={styles.meta}>
        {sessionQuery.isPending && "loading session…"}
        {sessionQuery.isError && "session unavailable"}
        {sessionQuery.data &&
          (RESIDENT_ACTORS.includes(actor)
            ? `contributor ${sessionQuery.data.contributor_id?.slice(0, 8)}…`
            : sessionQuery.data.namespace)}
      </span>
    </div>
  );
}

const visuallyHidden: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};
