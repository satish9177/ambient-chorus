import { useState } from "react";

import type { DemoActor } from "../../api/session";
import { useDeliverReplyMutation } from "../../hooks/useCommitment";
import { useCaseInvalidation } from "../../hooks/useCase";
import { ErrorBanner } from "../shared/ErrorBanner";
import { OperationProgress } from "../shared/OperationProgress";
import styles from "./ExternalReplyControl.module.css";

/**
 * A reviewed reply fixture selector, not a composer — the demo route never accepts a
 * caller-authored reply (08-api-design.md § External reply and verification).
 */
const DEFAULT_FIXTURE_ID = "manager-promise";

const REPLY_FIXTURES: { id: string; label: string; description: string }[] = [
  { id: DEFAULT_FIXTURE_ID, label: "Manager promise", description: "Unconditional, ISO date — the staged demo reply." },
  { id: "manager-hedge", label: "Manager hedge", description: "Hedged wording — extracts no commitment." },
  { id: "manager-weekday", label: "Manager weekday", description: "A weekday, not a deadline — extracts no commitment." },
  { id: "manager-quote-only", label: "Quoted text only", description: "Nothing but the original message quoted back." },
  { id: "manager-html-only", label: "HTML only", description: "No plain-text part — refused." },
  { id: "manager-attachment", label: "With attachment", description: "Carries an attachment — refused whole." },
];

export function ExternalReplyControl({ caseId, actor }: { caseId: string; actor: DemoActor }) {
  const mutation = useDeliverReplyMutation(caseId, actor);
  const invalidateCase = useCaseInvalidation(caseId);
  const [fixtureId, setFixtureId] = useState(DEFAULT_FIXTURE_ID);
  const [operationId, setOperationId] = useState<string | null>(null);

  return (
    <div className={styles.panel} aria-label="Deliver external reply">
      <label htmlFor="reply-fixture">Reply fixture</label>
      <select id="reply-fixture" value={fixtureId} onChange={(e) => setFixtureId(e.target.value)}>
        {REPLY_FIXTURES.map((fixture) => (
          <option key={fixture.id} value={fixture.id} title={fixture.description}>
            {fixture.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        className={styles.button}
        onClick={() =>
          mutation.mutate(
            { fixture_id: fixtureId },
            { onSuccess: (result) => setOperationId(result.operation_id) },
          )
        }
        disabled={mutation.isPending}
      >
        {mutation.isPending ? "Delivering…" : "Deliver reply"}
      </button>
      {mutation.isError && <ErrorBanner error={mutation.error} />}
      <OperationProgress
        operationId={operationId}
        actor={actor}
        label="Commitment extraction"
        onSucceeded={invalidateCase}
      />
    </div>
  );
}
