import { useQuery } from "@tanstack/react-query";

import { readSession } from "../api/endpoints";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";

export function useSessionQuery(actor: DemoActor) {
  return useQuery({
    queryKey: queryKeys.session(actor),
    queryFn: ({ signal }) => readSession({ actor, signal }),
  });
}
