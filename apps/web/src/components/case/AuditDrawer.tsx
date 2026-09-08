import { useState } from "react";

import type { AuditEvent } from "../../api/types";
import styles from "./AuditDrawer.module.css";

/** A simple chronological list — no timeline framework, no actor hashes, no raw payloads. */
export function AuditDrawer({ events }: { events: AuditEvent[] }) {
  const [open, setOpen] = useState(false);

  return (
    <section className={styles.drawer}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Hide" : "Show"} audit log ({events.length})
      </button>
      {open && (
        <ul className={styles.list}>
          {events.map((event) => (
            <li key={event.audit_event_id} className={styles.item}>
              <span>{event.event_type}</span>
              <span>{event.decision}</span>
              <span>{event.reason_codes.join(", ") || "—"}</span>
              <span>{new Date(event.occurred_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
