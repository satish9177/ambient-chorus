import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { readOperation } from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";
import type { OperationStatus } from "../api/types";

const FAST_POLL_MS = 1000;
const SLOW_POLL_MS = 5000;
const BACKOFF_AFTER_MS = 30_000;
const TIMEOUT_MS = 120_000;

const TERMINAL = new Set(["SUCCEEDED", "FAILED"]);

/**
 * Poll one operation to a terminal state (11-frontend-and-demo.md § Query and operation
 * behavior): every second while pending/running, back off to five seconds after thirty
 * seconds, and give up visibly — without cancelling server work — at two minutes.
 */
export function useOperationPoll(operationId: string | null, actor: DemoActor) {
  const [pastBackoff, setPastBackoff] = useState(false);
  const [timedOut, setTimedOut] = useState(false);

  useEffect(() => {
    // Resetting a countdown when the operation id changes, then scheduling its two timers,
    // is the effect's entire job — there is no external system to synchronize against here.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPastBackoff(false);
    setTimedOut(false);
    if (!operationId) return;
    const backoffTimer = setTimeout(() => setPastBackoff(true), BACKOFF_AFTER_MS);
    const timeoutTimer = setTimeout(() => setTimedOut(true), TIMEOUT_MS);
    return () => {
      clearTimeout(backoffTimer);
      clearTimeout(timeoutTimer);
    };
  }, [operationId]);

  const query = useQuery<OperationStatus>({
    queryKey: queryKeys.operation(operationId ?? "none"),
    queryFn: ({ signal }) => readOperation(operationId as string, { actor, signal }),
    enabled: operationId !== null,
    staleTime: 0,
    refetchInterval: (q) => {
      if (!operationId) return false;
      const data = q.state.data;
      if (data && TERMINAL.has(data.status)) return false;
      if (timedOut) return false;
      return pastBackoff ? SLOW_POLL_MS : FAST_POLL_MS;
    },
  });

  const status = query.data?.status;
  const isTerminal = status !== undefined && TERMINAL.has(status);

  return { ...query, isTerminal, timedOut: timedOut && !isTerminal };
}
