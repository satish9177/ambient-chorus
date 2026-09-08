import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { AuditDrawer } from "../components/case/AuditDrawer";
import { ApprovalPanel } from "../components/case/ApprovalPanel";
import { CaseActionsToolbar } from "../components/case/CaseActionsToolbar";
import { CaseStateStepper } from "../components/case/CaseStateStepper";
import { CommitmentTimeline } from "../components/case/CommitmentTimeline";
import { DemoClockControl } from "../components/case/DemoClockControl";
import { ExecutionStatusBanner } from "../components/case/ExecutionStatusBanner";
import { ExternalReplyControl } from "../components/case/ExternalReplyControl";
import { MandateContributorLinks } from "../components/case/MandateContributorLinks";
import { PrivacyBoundaryCompare } from "../components/case/PrivacyBoundaryCompare";
import { VerificationPanel } from "../components/case/VerificationPanel";
import { PrivateInvestigationPanel } from "../components/private/PrivateInvestigationPanel";
import { ActionProposalPreview } from "../components/shareable/ActionProposalPreview";
import { ShareableExternalViewPanel } from "../components/shareable/ShareableExternalViewPanel";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { useDemoCase } from "../context/DemoCaseContext";
import { usePersona } from "../context/PersonaContext";
import { useAuditQuery, useCaseQuery, useInvestigationQuery } from "../hooks/useCase";

const DEMO_START_LOGICAL_NOW = "2030-01-14T09:00:00.000000Z";

export function CaseActionPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { actor } = usePersona();
  const { setCaseId } = useDemoCase();

  // The case id is already public in the URL the moment this route renders — recording it here
  // (P2-3) is what lets a resident persona reach `/mandates/:contributorId?case=:caseId`
  // through a real "My mandate" link instead of a URL a test has to construct. That is
  // navigation state, not authorization state: nothing about who may *read* the case is
  // retained.
  //
  // Every HTTP request this page issues — the base case read, the investigation, the audit —
  // is made as the *currently active* `actor` (P1-1). There is no `readerActor`, no retained
  // presenter identity, no privileged fallback. A resident persona that opens this route
  // issues the case read as itself and the backend returns the *safe subset* it permits that
  // resident (no private title, evidence summary, or privacy counts) — never a presenter
  // projection served through a remembered identity. The per-persona query key
  // (`queryKeys.case(caseId, actor)`) makes a persona switch always a different cache entry,
  // so a cached presenter projection is never rendered as resident-visible state and a delayed
  // presenter response cannot overwrite a newer resident state.
  useEffect(() => {
    if (caseId) setCaseId(caseId);
  }, [caseId, setCaseId]);

  const isPresenterActive = actor === "presenter_admin";
  const isApprover = actor === "case_approver";

  const caseQuery = useCaseQuery(caseId ?? null, actor);
  const investigationQuery = useInvestigationQuery(caseId ?? null, actor, isPresenterActive);
  const auditQuery = useAuditQuery(caseId ?? null, actor, isPresenterActive);

  const [logicalNow, setLogicalNow] = useState(DEMO_START_LOGICAL_NOW);

  if (!caseId) {
    return <ErrorBanner error={new Error("No case selected.")} />;
  }

  if (caseQuery.isPending) return <p>Loading case…</p>;
  if (caseQuery.isError) return <ErrorBanner error={caseQuery.error} />;

  const surface = caseQuery.data;
  const currentAction = surface.current_action;
  const dueCommitment = surface.commitments.find((c) => c.status === "DUE");
  const pendingCommitment = surface.commitments.find((c) => c.status === "PENDING");

  return (
    <section aria-labelledby="case-heading">
      <h2 id="case-heading">{surface.case?.title ?? "Case"}</h2>
      <CaseStateStepper
        state={surface.case?.state ?? "CANDIDATE"}
        reasonCode={surface.case?.state_reason_code}
      />

      {isPresenterActive && <CaseActionsToolbar caseId={caseId} caseSurface={surface} actor={actor} />}

      {isPresenterActive && (
        <MandateContributorLinks caseId={caseId} rows={surface.evidence_summary ?? []} />
      )}

      <PrivacyBoundaryCompare
        left={
          isPresenterActive ? (
            <PrivateInvestigationPanel investigation={investigationQuery.data ?? null} />
          ) : (
            <div>Private investigation is presenter-only.</div>
          )
        }
        right={
          <ShareableExternalViewPanel
            view={surface.current_shareable_view}
            privacyCounts={surface.privacy_counts}
          />
        }
      />

      {currentAction && (
        <>
          <ActionProposalPreview
            action={currentAction}
            destination={surface.current_shareable_view?.destination ?? null}
          />
          <ApprovalPanel
            caseId={caseId}
            action={currentAction}
            actor={actor}
            isApprover={isApprover}
            caseRefetching={caseQuery.isFetching}
          />
          <ExecutionStatusBanner
            caseId={caseId}
            actionId={currentAction.action_id}
            execution={currentAction.execution}
            approvalAuthorizationCurrent={currentAction.approval_authorization_current}
            caseRefetching={caseQuery.isFetching}
            isApprover={isApprover}
            actor={actor}
          />
        </>
      )}

      {isPresenterActive && surface.case?.state === "ACTIONED" && (
        <ExternalReplyControl caseId={caseId} actor={actor} />
      )}

      <h3>Commitments</h3>
      <CommitmentTimeline commitments={surface.commitments} />

      {isPresenterActive && pendingCommitment && (
        <DemoClockControl
          caseId={caseId}
          commitment={pendingCommitment}
          logicalNow={logicalNow}
          actor={actor}
          onAdvanced={setLogicalNow}
        />
      )}

      {dueCommitment && (
        <VerificationPanel caseId={caseId} commitment={dueCommitment} actor={actor} />
      )}

      {isPresenterActive && !auditQuery.isError && auditQuery.data && (
        <AuditDrawer events={auditQuery.data.items} />
      )}
    </section>
  );
}
