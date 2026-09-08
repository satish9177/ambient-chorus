import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";

import { ingestMessages, readDemoCorpus, readFeed, resetDemo } from "../api/endpoints";
import { newIdempotencyKey } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { DemoActor } from "../api/session";

export function useFeedQuery(communityId: string | null, actor: DemoActor) {
  return useQuery({
    queryKey: queryKeys.feed(communityId ?? "none"),
    queryFn: ({ signal }) =>
      readFeed({ community_id: communityId as string, limit: 50 }, { actor, signal }),
    enabled: communityId !== null,
  });
}

/**
 * The Monitor operation reaching a terminal state is what actually changes the feed (a
 * `chorus_signal` appears once it succeeds) — the ingest POST settling only means the request
 * was accepted (202), not that the Monitor has finished (P2-6). Call this from the operation
 * poll's own terminal callback, not from the mutation's `onSettled`.
 */
export function useFeedInvalidation(communityId: string | null) {
  const queryClient = useQueryClient();
  return () => {
    if (communityId) void queryClient.invalidateQueries({ queryKey: queryKeys.feed(communityId) });
  };
}

/** One reset intent, one key, for as long as the presenter keeps retrying this same click. */
export function useResetMutation(actor: DemoActor) {
  const queryClient = useQueryClient();
  const keyRef = useRef<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      keyRef.current ??= newIdempotencyKey();
      return resetDemo(
        { namespace: "DEMO", confirm: "RESET DEMO", seed_version: "elevator/v1" },
        { actor, idempotencyKey: keyRef.current },
      );
    },
    onSuccess: () => {
      keyRef.current = null;
      void queryClient.invalidateQueries();
    },
  });

  return mutation;
}

/**
 * Replays the seeded corpus so the Monitor runs. The corpus itself is read from
 * `GET /demo/corpus` (P2-7) — the server's own copy of the exact same
 * `SyntheticAmbientAdapter` reset seeds from — rather than a frontend-maintained mirror of the
 * fixture file that could silently drift from it. No message id, content hash, or case id is
 * computed here; the server owns all of that.
 *
 * This mutation only reports that `POST /ingest/messages` was accepted (202) — the feed does
 * not actually change until the Monitor operation it starts reaches `SUCCEEDED` (P2-6), so the
 * feed invalidation belongs on that operation's terminal callback (`useFeedInvalidation`,
 * wired to `OperationProgress`'s `onSucceeded` in `DemoResetControl`), not here.
 */
export function useStartDiscoveryMutation(communityId: string | null, actor: DemoActor) {
  const keyRef = useRef<string | null>(null);

  return useMutation({
    mutationFn: async () => {
      if (!communityId) throw new Error("no community to ingest into");
      const corpus = await readDemoCorpus({ actor });
      keyRef.current ??= newIdempotencyKey();
      return ingestMessages(
        { community_id: corpus.community_id, messages: corpus.messages },
        { actor, idempotencyKey: keyRef.current },
      );
    },
    onSuccess: () => {
      keyRef.current = null;
    },
  });
}
