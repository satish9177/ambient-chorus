import { useQuery, useQueryClient } from "@tanstack/react-query";

import { readCase, readCaseAudit, readInvestigation } from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";

export function useCaseQuery(caseId: string | null, actor: DemoActor | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.case(caseId ?? "none", actor ?? "none"),
    queryFn: ({ signal }) => readCase(caseId as string, { actor: actor as DemoActor, signal }),
    enabled: caseId !== null && actor !== null && enabled,
  });
}

export function useInvestigationQuery(caseId: string | null, actor: DemoActor, enabled = true) {
  return useQuery({
    queryKey: queryKeys.investigation(caseId ?? "none"),
    queryFn: ({ signal }) => readInvestigation(caseId as string, { actor, signal }),
    enabled: caseId !== null && enabled,
  });
}

export function useAuditQuery(caseId: string | null, actor: DemoActor, enabled = true) {
  return useQuery({
    queryKey: queryKeys.audit(caseId ?? "none"),
    queryFn: ({ signal }) => readCaseAudit(caseId as string, {}, { actor, signal }),
    enabled: caseId !== null && enabled,
  });
}

/** Invalidate every surface a case mutation could have moved. */
export function useCaseInvalidation(caseId: string | null) {
  const queryClient = useQueryClient();
  return () => {
    if (!caseId) return;
    void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
    void queryClient.invalidateQueries({ queryKey: queryKeys.investigation(caseId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.audit(caseId) });
  };
}
