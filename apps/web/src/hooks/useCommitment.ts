import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { newIdempotencyKey } from "../api/client";
import { advanceDemoClock, deliverExternalReply, verifyCommitment } from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";
import type { OpBody } from "../api/types";
import { handleMutationError } from "./mutationHelpers";

/**
 * `POST /demo/external-replies` returns 202 — the commitment does not exist until the
 * `EXTRACT_COMMITMENT` operation it starts reaches `SUCCEEDED`, so invalidation belongs on that
 * operation's terminal callback (P2-6), not here. See `ExternalReplyControl`'s
 * `onSucceeded={invalidateCase}`.
 */
export function useDeliverReplyMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const keyRef = useRef<string | null>(null);

  return useMutation({
    mutationFn: (body: OpBody<"deliver_external_reply_v1_demo_external_replies_post">) => {
      keyRef.current ??= newIdempotencyKey();
      return deliverExternalReply(body, { actor, idempotencyKey: keyRef.current });
    },
    onSuccess: () => {
      keyRef.current = null;
    },
    onError: (error) =>
      handleMutationError(error, queryClient, () => (keyRef.current = null), [
        ["case", caseId],
      ]),
  });
}

export function useAdvanceClockMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: OpBody<"advance_demo_clock_v1_demo_clock_advance_post">) =>
      advanceDemoClock(body, { actor }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    },
  });
}

export function useVerifyCommitmentMutation(
  caseId: string,
  commitmentId: string,
  actor: DemoActor,
) {
  const queryClient = useQueryClient();
  const keyRef = useRef<string | null>(null);

  return useMutation({
    mutationFn: (
      body: OpBody<"verify_commitment_v1_cases__case_id__commitments__commitment_id__verification_post">,
    ) => {
      keyRef.current ??= newIdempotencyKey();
      return verifyCommitment(caseId, commitmentId, body, {
        actor,
        idempotencyKey: keyRef.current,
      });
    },
    onSuccess: () => {
      keyRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.audit(caseId) });
    },
    onError: (error) =>
      handleMutationError(error, queryClient, () => (keyRef.current = null), [
        ["case", caseId],
        queryKeys.audit(caseId),
      ]),
  });
}
