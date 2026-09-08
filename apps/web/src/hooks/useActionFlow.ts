import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { newIdempotencyKey } from "../api/client";
import {
  approveAction,
  compileView,
  invalidateAction,
  proposeAction,
  startExecution,
  startInvestigation,
} from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";
import type { OpBody } from "../api/types";
import { handleMutationError } from "./mutationHelpers";

function useIntentKey() {
  const ref = useRef<string | null>(null);
  const get = () => (ref.current ??= newIdempotencyKey());
  const reset = () => {
    ref.current = null;
  };
  return { get, reset };
}

/**
 * `POST .../investigations` returns 202 — the investigation itself doesn't exist until that
 * operation reaches `SUCCEEDED`, so invalidating the case here at "request accepted" time would
 * refetch data that hasn't changed yet. The real invalidation belongs on the operation's own
 * terminal callback (P2-6) — see `CaseActionsToolbar`'s `onSucceeded={invalidateCase}`.
 */
export function useStartInvestigationMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"start_investigation_v1_cases__case_id__investigations_post">) =>
      startInvestigation(caseId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => key.reset(),
    onError: (error) => handleMutationError(error, queryClient, key.reset, [["case", caseId]]),
  });
}

export function useCompileViewMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"compile_view_v1_cases__case_id__views_post">) =>
      compileView(caseId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => {
      key.reset();
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.investigation(caseId) });
    },
    onError: (error) =>
      handleMutationError(error, queryClient, key.reset, [
        ["case", caseId],
        queryKeys.investigation(caseId),
      ]),
  });
}

/** Also 202 — see `useStartInvestigationMutation`'s note; `onSucceeded` owns invalidation. */
export function useProposeActionMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"propose_action_v1_cases__case_id__actions_post">) =>
      proposeAction(caseId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => key.reset(),
    onError: (error) => handleMutationError(error, queryClient, key.reset, [["case", caseId]]),
  });
}

export function useApproveActionMutation(caseId: string, actionId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"approve_action_v1_cases__case_id__actions__action_id__approvals_post">) =>
      approveAction(caseId, actionId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => {
      key.reset();
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    },
    // A rejected/stale proposal_hash, preview_hash, or expected_execution_version all surface
    // as this same conflict class (08-api-design.md § Propose, approve, execute): refetch the
    // case so the next render shows the *current* action, never resubmit the approval body.
    onError: (error) => handleMutationError(error, queryClient, key.reset, [["case", caseId]]),
  });
}

export function useInvalidateActionMutation(caseId: string, actionId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"invalidate_action_v1_cases__case_id__actions__action_id__invalidation_post">) =>
      invalidateAction(caseId, actionId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => {
      key.reset();
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    },
    onError: (error) => handleMutationError(error, queryClient, key.reset, [["case", caseId]]),
  });
}

/** Also 202 — see `useStartInvestigationMutation`'s note; `onSucceeded` owns invalidation. */
export function useStartExecutionMutation(caseId: string, actionId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const key = useIntentKey();

  return useMutation({
    mutationFn: (body: OpBody<"start_execution_v1_cases__case_id__actions__action_id__executions_post">) =>
      startExecution(caseId, actionId, body, { actor, idempotencyKey: key.get() }),
    onSuccess: () => key.reset(),
    onError: (error) => handleMutationError(error, queryClient, key.reset, [["case", caseId]]),
  });
}
