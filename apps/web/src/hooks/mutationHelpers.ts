import type { QueryClient } from "@tanstack/react-query";

import { isStaleConflict } from "../api/client";

/**
 * The shared P2-5 fail-closed behavior for every mutation that submits an OCC version or a
 * committed hash: on a stale/version/hash conflict, discard the idempotency key that named the
 * now-obsolete intent (a fresh attempt after refetching is a *different* command, so it must
 * get a different key) and refetch every query the stale write could have been judged against,
 * so the next control the human sees reflects the server's current state. The mutation is never
 * resubmitted automatically — the refetch is the only side effect here.
 */
export function handleMutationError(
  error: unknown,
  queryClient: QueryClient,
  resetKey: () => void,
  affectedQueryKeys: readonly (readonly unknown[])[],
): void {
  // A network failure keeps its key — 11-frontend-and-demo.md's retry rule is that a client
  // retry of a *transport* failure reuses the same key, so the eventual attempt still lands as
  // one command. Only a conflict, which means the command itself is now judged against stale
  // state, discards it: any further attempt has to be a new command against fresh data.
  if (!isStaleConflict(error)) return;
  resetKey();
  for (const queryKey of affectedQueryKeys) {
    void queryClient.invalidateQueries({ queryKey: queryKey as unknown[] });
  }
}
