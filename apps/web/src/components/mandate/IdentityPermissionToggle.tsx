import type { IdentityPermission } from "../../api/types";
import { DisclosureScopeSelect, type DisclosureScope } from "./DisclosureScopeSelect";
import styles from "./FactPermissionRow.module.css";

/**
 * Identity permission is a separate decision from content permission — never one generic
 * consent checkbox (11-frontend-and-demo.md § Private Mandate Thread).
 */
export function IdentityPermissionToggle({
  identity,
  editable,
  externallyShareable,
  scope,
  onChangeShareable,
  onChangeScope,
}: {
  identity: IdentityPermission;
  editable: boolean;
  externallyShareable: boolean;
  scope: DisclosureScope;
  onChangeShareable: (value: boolean) => void;
  onChangeScope: (value: DisclosureScope) => void;
}) {
  return (
    <div className={styles.row}>
      <div>
        <p className={styles.wording}>Your identity</p>
        <div className={styles.meta}>
          <label>
            <input
              type="checkbox"
              checked={externallyShareable}
              disabled={!editable}
              onChange={(event) => onChangeShareable(event.target.checked)}
            />{" "}
            May be shared outside this case
          </label>
          <span>Policy maximum: {identity.policy_maximum_scope}</span>
        </div>
      </div>
      <div className={styles.control}>
        <label htmlFor="identity-scope">Identity scope</label>
        <DisclosureScopeSelect
          id="identity-scope"
          value={scope}
          maxAllowed={identity.policy_maximum_scope as DisclosureScope}
          disabled={!editable}
          onChange={onChangeScope}
        />
      </div>
    </div>
  );
}
