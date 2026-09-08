/** The frozen query keys (11-frontend-and-demo.md § Query and operation behavior). */
export const queryKeys = {
  session: (actor: string) => ["session", actor] as const,
  feed: (communityId: string) => ["feed", communityId] as const,
  case: (caseId: string, actorView: string) => ["case", caseId, actorView] as const,
  investigation: (caseId: string) => ["investigation", caseId] as const,
  // The frozen key is `['mandate', contributorId, caseId]`, but that lets a persona switch
  // that keeps the same route param (same `contributorId` in the URL, different active actor)
  // reuse a cached read fetched under a *different* resident's authorization without issuing a
  // new request or showing a loading state (P1-2) — TanStack Query only refetches when the key
  // itself changes. `actor` is added so a persona switch is always a different cache entry.
  mandate: (contributorId: string, caseId: string, actor: string) =>
    ["mandate", contributorId, caseId, actor] as const,
  audit: (caseId: string) => ["audit", caseId] as const,
  operation: (operationId: string) => ["operation", operationId] as const,
};
