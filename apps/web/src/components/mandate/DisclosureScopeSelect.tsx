import type { MandateDecisionKind } from "../../api/types";

export type DisclosureScope = MandateDecisionKind;

// Least to most permissive. The generated schema only enumerates the set (`FactGrantRequest`);
// the ordering is domain knowledge this UI needs to compute an allowed prefix.
const SCOPE_ORDER: readonly DisclosureScope[] = [
  "INTERNAL_ONLY",
  "AGGREGATE_ONLY",
  "ANONYMOUS_CASE",
  "NAMED_CASE",
  "EXTERNAL_ACTION",
] as const;

const SCOPE_LABELS: Record<DisclosureScope, string> = {
  INTERNAL_ONLY: "Internal only — never leaves this case",
  AGGREGATE_ONLY: "Aggregate only — counted, not quoted",
  ANONYMOUS_CASE: "Anonymous case — used, not attributed",
  NAMED_CASE: "Named within case",
  EXTERNAL_ACTION: "External action — may be sent out",
};

/** One scope picker, capped at the policy maximum. Never a generic yes/no consent checkbox. */
export function DisclosureScopeSelect({
  id,
  value,
  maxAllowed,
  disabled,
  onChange,
}: {
  id: string;
  value: DisclosureScope;
  maxAllowed: DisclosureScope;
  disabled?: boolean;
  onChange: (next: DisclosureScope) => void;
}) {
  const ceilingIndex = SCOPE_ORDER.indexOf(maxAllowed);
  const allowed = SCOPE_ORDER.slice(0, ceilingIndex + 1);

  return (
    <select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as DisclosureScope)}
    >
      {allowed.map((scope) => (
        <option key={scope} value={scope}>
          {SCOPE_LABELS[scope]}
        </option>
      ))}
    </select>
  );
}

export { SCOPE_LABELS, SCOPE_ORDER };
