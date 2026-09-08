import { useState } from "react";

import type { DemoActor } from "../../api/session";
import type { CaseSurface, EvidenceSummaryRow } from "../../api/types";
import {
  useCompileViewMutation,
  useProposeActionMutation,
  useStartInvestigationMutation,
} from "../../hooks/useActionFlow";
import { useCaseInvalidation } from "../../hooks/useCase";
import { useProposeMandatesMutation } from "../../hooks/useMandate";
import { ErrorBanner } from "../shared/ErrorBanner";
import { OperationProgress } from "../shared/OperationProgress";
import styles from "./ExternalReplyControl.module.css";

function requestedFactsFrom(rows: EvidenceSummaryRow[]) {
  return rows.map((row) => ({
    fact_id: row.fact_id,
    necessity: (row.fact_type === "INCIDENT_OCCURRENCE" ? "REQUIRED" : "OPTIONAL") as
      | "REQUIRED"
      | "OPTIONAL",
    intended_usage: (row.fact_type === "INCIDENT_OCCURRENCE" ? "CLAIM" : "AGGREGATION_INPUT") as
      | "CLAIM"
      | "AGGREGATION_INPUT",
  }));
}

export function CaseActionsToolbar({
  caseId,
  caseSurface,
  actor,
}: {
  caseId: string;
  caseSurface: CaseSurface;
  actor: DemoActor;
}) {
  const proposeMandates = useProposeMandatesMutation(caseId, actor);
  const startInvestigation = useStartInvestigationMutation(caseId, actor);
  const compileView = useCompileViewMutation(caseId, actor);
  const proposeAction = useProposeActionMutation(caseId, actor);
  const invalidateCase = useCaseInvalidation(caseId);
  const [investigationOpId, setInvestigationOpId] = useState<string | null>(null);
  const [proposeActionOpId, setProposeActionOpId] = useState<string | null>(null);

  const state = caseSurface.case?.state;
  const version = caseSurface.case?.version ?? 0;

  return (
    <div className={styles.panel} aria-label="Case actions">
      {state === "CANDIDATE" && (
        <button
          type="button"
          className={styles.button}
          disabled={proposeMandates.isPending}
          onClick={() => proposeMandates.mutate(version)}
        >
          {proposeMandates.isPending ? "Proposing…" : "Propose mandates"}
        </button>
      )}

      <button
        type="button"
        className={styles.button}
        disabled={startInvestigation.isPending || state === "CANDIDATE"}
        onClick={() =>
          startInvestigation.mutate(
            { expected_case_version: version, reason: "INITIAL" },
            { onSuccess: (result) => setInvestigationOpId(result.operation_id) },
          )
        }
      >
        {startInvestigation.isPending ? "Starting…" : "Run investigation"}
      </button>
      <OperationProgress
        operationId={investigationOpId}
        actor={actor}
        label="Investigation"
        onSucceeded={invalidateCase}
      />

      <button
        type="button"
        className={styles.button}
        disabled={compileView.isPending || (caseSurface.evidence_summary ?? []).length === 0}
        onClick={() => {
          const compileId = crypto.randomUUID();
          compileView.mutate({
            compile_id: compileId,
            expected_case_version: version,
            requested_facts: requestedFactsFrom(caseSurface.evidence_summary ?? []),
            requested_evidence_ids: [],
            purpose: "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
          });
        }}
      >
        {compileView.isPending ? "Compiling…" : "Compile shareable view"}
      </button>

      <button
        type="button"
        className={styles.button}
        disabled={proposeAction.isPending || !caseSurface.current_shareable_view}
        onClick={() => {
          const view = caseSurface.current_shareable_view;
          if (!view) return;
          proposeAction.mutate(
            { expected_case_version: version, view_id: view.view_id, view_hash: view.view_hash },
            { onSuccess: (result) => setProposeActionOpId(result.operation_id) },
          );
        }}
      >
        {proposeAction.isPending ? "Proposing…" : "Propose action"}
      </button>
      <OperationProgress
        operationId={proposeActionOpId}
        actor={actor}
        label="Action proposal"
        onSucceeded={invalidateCase}
      />

      {proposeMandates.isError && <ErrorBanner error={proposeMandates.error} />}
      {startInvestigation.isError && <ErrorBanner error={startInvestigation.error} />}
      {compileView.isError && <ErrorBanner error={compileView.error} />}
      {proposeAction.isError && <ErrorBanner error={proposeAction.error} />}
    </div>
  );
}
