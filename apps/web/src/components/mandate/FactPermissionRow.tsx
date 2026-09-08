import type { FactPermission } from "../../api/types";
import { DisclosureScopeSelect, SCOPE_LABELS, type DisclosureScope } from "./DisclosureScopeSelect";
import styles from "./FactPermissionRow.module.css";

export function FactPermissionRow({
  fact,
  editable,
  value,
  onChange,
}: {
  fact: FactPermission;
  editable: boolean;
  value: DisclosureScope;
  onChange: (next: DisclosureScope) => void;
}) {
  const inputId = `scope-${fact.fact_id}`;
  return (
    <div className={styles.row}>
      <div>
        <p className={styles.wording}>{fact.wording}</p>
        <div className={styles.meta}>
          <span>Proposed: {SCOPE_LABELS[fact.proposed_scope as DisclosureScope]}</span>
          <span>Policy maximum: {SCOPE_LABELS[fact.policy_maximum_scope as DisclosureScope]}</span>
        </div>
        {fact.locked_reason && <p className={styles.locked}>Locked: {fact.locked_reason}</p>}
      </div>
      <div className={styles.control}>
        <label htmlFor={inputId}>Content scope</label>
        <DisclosureScopeSelect
          id={inputId}
          value={value}
          maxAllowed={fact.policy_maximum_scope as DisclosureScope}
          disabled={!editable || fact.locked_reason !== null}
          onChange={onChange}
        />
      </div>
    </div>
  );
}
