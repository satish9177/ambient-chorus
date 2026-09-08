import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { newIdempotencyKey } from "../api/client";
import { decideMandate, proposeMandates, readCurrentMandate } from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";
import type { OpBody } from "../api/types";
import { handleMutationError } from "./mutationHelpers";

export function useMandateThreadQuery(
  contributorId: string | null,
  caseId: string | null,
  actor: DemoActor,
) {
  return useQuery({
    queryKey: queryKeys.mandate(contributorId ?? "none", caseId ?? "none", actor),
    queryFn: ({ signal }) =>
      readCurrentMandate(contributorId as string, caseId as string, { actor, signal }),
    enabled: contributorId !== null && caseId !== null,
    // A persona switch must never render the previous persona's cached rows while the new
    // read is in flight (P1-2) — the actor-scoped key above already forces a fresh fetch, and
    // leaving TanStack Query's default (no `placeholderData: keepPreviousData`) means this
    // query reports `isPending`/`data: undefined` for that fetch rather than reusing another
    // actor's last-fetched thread.
  });
}

export function useProposeMandatesMutation(caseId: string, actor: DemoActor) {
  const queryClient = useQueryClient();
  const keyRef = useRef<string | null>(null);

  return useMutation({
    mutationFn: (expectedCaseVersion: number) => {
      keyRef.current ??= newIdempotencyKey();
      return proposeMandates(
        caseId,
        { expected_case_version: expectedCaseVersion },
        { actor, idempotencyKey: keyRef.current },
      );
    },
    onSuccess: () => {
      keyRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    },
    onError: (error) =>
      handleMutationError(error, queryClient, () => (keyRef.current = null), [
        ["case", caseId],
      ]),
  });
}

type DecisionBody = OpBody<"decide_mandate_v1_cases__case_id__mandates__mandate_id__decisions_post">;

export function useDecideMandateMutation(
  caseId: string,
  mandateId: string,
  contributorId: string,
  actor: DemoActor,
) {
  const queryClient = useQueryClient();
  const keyRef = useRef<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: DecisionBody) => {
      keyRef.current ??= newIdempotencyKey();
      return decideMandate(caseId, mandateId, body, { actor, idempotencyKey: keyRef.current });
    },
    onSuccess: () => {
      keyRef.current = null;
      // Prefix match on the first three segments invalidates this contributor's thread under
      // every actor it was ever fetched as (there should only ever be one: the resident
      // themselves, or the presenter's read-only view before P1-2).
      void queryClient.invalidateQueries({ queryKey: ["mandate", contributorId, caseId] });
      void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    },
    onError: (error) =>
      handleMutationError(error, queryClient, () => { keyRef.current = null; }, [
        ["mandate", contributorId, caseId],
        ["case", caseId],
      ]),
  });

  return mutation;
}
