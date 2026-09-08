/**
 * One typed function per operation. Every request/response shape is `OpBody`/`OpResponse`
 * over the generated `operations` map — nothing here is a hand-written wire type.
 */
import { apiGet, apiPost, type RequestOptions } from "./client";
import type { OpBody, OpResponse } from "./types";
import type { DemoActor } from "./session";

type Base = { actor: DemoActor; signal?: AbortSignal };
type Mutating = Base & { idempotencyKey: string };

// -- Demo / session ---------------------------------------------------------------------

export function resetDemo(
  body: OpBody<"reset_demo_v1_demo_reset_post">,
  opts: Base & { idempotencyKey?: string },
): Promise<OpResponse<"reset_demo_v1_demo_reset_post">> {
  return apiPost("/demo/reset", { ...opts, body });
}

export function readSession(
  opts: Base,
): Promise<OpResponse<"read_session_v1_session_get">> {
  return apiGet("/session", opts);
}

export function readDemoCorpus(
  opts: Base,
): Promise<OpResponse<"read_demo_corpus_v1_demo_corpus_get">> {
  return apiGet("/demo/corpus", opts);
}

export function advanceDemoClock(
  body: OpBody<"advance_demo_clock_v1_demo_clock_advance_post">,
  opts: Base,
): Promise<OpResponse<"advance_demo_clock_v1_demo_clock_advance_post">> {
  return apiPost("/demo/clock/advance", { ...opts, body });
}

export function deliverExternalReply(
  body: OpBody<"deliver_external_reply_v1_demo_external_replies_post">,
  opts: Mutating,
): Promise<OpResponse<"deliver_external_reply_v1_demo_external_replies_post", 202>> {
  return apiPost("/demo/external-replies", { ...opts, body });
}

// -- Feed / ingest ------------------------------------------------------------------------

export function readFeed(
  query: { community_id: string; limit?: number; cursor?: string },
  opts: Base,
): Promise<OpResponse<"read_feed_v1_feed_get">> {
  return apiGet("/feed", { ...opts, query });
}

export function ingestMessages(
  body: OpBody<"ingest_messages_v1_ingest_messages_post">,
  opts: Mutating,
): Promise<OpResponse<"ingest_messages_v1_ingest_messages_post", 202>> {
  return apiPost("/ingest/messages", { ...opts, body });
}

// -- Operations ---------------------------------------------------------------------------

export function readOperation(
  operationId: string,
  opts: Base,
): Promise<OpResponse<"read_operation_v1_operations__operation_id__get">> {
  return apiGet(`/operations/${operationId}`, opts);
}

// -- Case surface -------------------------------------------------------------------------

export function readCase(
  caseId: string,
  opts: Base,
): Promise<OpResponse<"read_case_v1_cases__case_id__get">> {
  return apiGet(`/cases/${caseId}`, opts);
}

export function readInvestigation(
  caseId: string,
  opts: Base,
): Promise<OpResponse<"read_investigation_v1_cases__case_id__investigation_get">> {
  return apiGet(`/cases/${caseId}/investigation`, opts);
}

export function readCaseAudit(
  caseId: string,
  query: { limit?: number; cursor?: string },
  opts: Base,
): Promise<OpResponse<"read_case_audit_v1_cases__case_id__audit_get">> {
  return apiGet(`/cases/${caseId}/audit`, { ...opts, query });
}

// -- Mandates -----------------------------------------------------------------------------

export function proposeMandates(
  caseId: string,
  body: OpBody<"propose_mandates_v1_cases__case_id__mandates_post">,
  opts: Mutating,
): Promise<OpResponse<"propose_mandates_v1_cases__case_id__mandates_post">> {
  return apiPost(`/cases/${caseId}/mandates`, { ...opts, body });
}

export function readCurrentMandate(
  contributorId: string,
  caseId: string,
  opts: Base,
): Promise<OpResponse<"read_current_mandate_v1_contributors__contributor_id__mandates_current_get">> {
  return apiGet(`/contributors/${contributorId}/mandates/current`, {
    ...opts,
    query: { case_id: caseId },
  });
}

export function decideMandate(
  caseId: string,
  mandateId: string,
  body: OpBody<"decide_mandate_v1_cases__case_id__mandates__mandate_id__decisions_post">,
  opts: Mutating,
): Promise<OpResponse<"decide_mandate_v1_cases__case_id__mandates__mandate_id__decisions_post">> {
  return apiPost(`/cases/${caseId}/mandates/${mandateId}/decisions`, { ...opts, body });
}

// -- Investigation --------------------------------------------------------------------------

export function startInvestigation(
  caseId: string,
  body: OpBody<"start_investigation_v1_cases__case_id__investigations_post">,
  opts: Mutating,
): Promise<OpResponse<"start_investigation_v1_cases__case_id__investigations_post", 202>> {
  return apiPost(`/cases/${caseId}/investigations`, { ...opts, body });
}

// -- Compile ------------------------------------------------------------------------------

export function compileView(
  caseId: string,
  body: OpBody<"compile_view_v1_cases__case_id__views_post">,
  opts: Mutating,
): Promise<OpResponse<"compile_view_v1_cases__case_id__views_post">> {
  return apiPost(`/cases/${caseId}/views`, { ...opts, body });
}

// -- Action / approval / execution -----------------------------------------------------------

export function proposeAction(
  caseId: string,
  body: OpBody<"propose_action_v1_cases__case_id__actions_post">,
  opts: Mutating,
): Promise<OpResponse<"propose_action_v1_cases__case_id__actions_post", 202>> {
  return apiPost(`/cases/${caseId}/actions`, { ...opts, body });
}

export function approveAction(
  caseId: string,
  actionId: string,
  body: OpBody<"approve_action_v1_cases__case_id__actions__action_id__approvals_post">,
  opts: Mutating,
): Promise<OpResponse<"approve_action_v1_cases__case_id__actions__action_id__approvals_post">> {
  return apiPost(`/cases/${caseId}/actions/${actionId}/approvals`, { ...opts, body });
}

export function invalidateAction(
  caseId: string,
  actionId: string,
  body: OpBody<"invalidate_action_v1_cases__case_id__actions__action_id__invalidation_post">,
  opts: Mutating,
): Promise<OpResponse<"invalidate_action_v1_cases__case_id__actions__action_id__invalidation_post">> {
  return apiPost(`/cases/${caseId}/actions/${actionId}/invalidation`, { ...opts, body });
}

export function startExecution(
  caseId: string,
  actionId: string,
  body: OpBody<"start_execution_v1_cases__case_id__actions__action_id__executions_post">,
  opts: Mutating,
): Promise<
  OpResponse<"start_execution_v1_cases__case_id__actions__action_id__executions_post", 202>
> {
  return apiPost(`/cases/${caseId}/actions/${actionId}/executions`, { ...opts, body });
}

// -- Commitments / verification ---------------------------------------------------------------

export function verifyCommitment(
  caseId: string,
  commitmentId: string,
  body: OpBody<"verify_commitment_v1_cases__case_id__commitments__commitment_id__verification_post">,
  opts: Mutating,
): Promise<
  OpResponse<"verify_commitment_v1_cases__case_id__commitments__commitment_id__verification_post">
> {
  return apiPost(`/cases/${caseId}/commitments/${commitmentId}/verification`, { ...opts, body });
}

export type { RequestOptions };
