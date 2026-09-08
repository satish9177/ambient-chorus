import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockFetchByRoute, type MockedCall } from "../test/mockFetch";
import { useStartDiscoveryMutation } from "./useFeed";

/**
 * P2-7: the frontend must have no fixture mirror of its own — whatever `GET /demo/corpus`
 * returns is exactly what gets replayed to `POST /ingest/messages`, unmodified. This test
 * uses a corpus the real fixture has never contained, which is only possible to pass if the
 * discovery flow has no hardcoded message data left in it.
 */
describe("useStartDiscoveryMutation reads the corpus from the server, never a frontend copy", () => {
  it("replays exactly what GET /demo/corpus returned, including a message no real fixture has", async () => {
    const fictionalMessage = {
      adapter: "SYNTHETIC" as const,
      channel_message_id: "fictional-only-999",
      contributor_id: null,
      sent_at: "2099-01-01T00:00:00.000000Z",
      text: "This message exists only in this test's mock, never in the real fixture.",
      attachments: [],
    };

    const { calls } = mockFetchByRoute([
      (call: MockedCall) => {
        if (!call.url.includes("/demo/corpus")) return undefined;
        return {
          status: 200,
          body: {
            seed_version: "elevator/v1",
            corpus_sha256: "sha256:" + "0".repeat(64),
            community_id: "comm-fictional-1",
            messages: [fictionalMessage],
          },
        };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/ingest/messages")) return undefined;
        return {
          status: 202,
          body: {
            messages: [{ channel_message_id: "fictional-only-999", message_id: "m1", replay: false }],
            accepted_count: 1,
            replayed_count: 0,
            operation: { operation_id: "op-1", status: "PENDING", poll_url: "/v1/operations/op-1" },
          },
        };
      },
    ]);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { result } = renderHook(() => useStartDiscoveryMutation("comm-fictional-1", "presenter_admin"), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const ingestCall = calls.find((c) => c.url.includes("/ingest/messages"))!;
    expect(ingestCall.body).toEqual({
      community_id: "comm-fictional-1",
      messages: [fictionalMessage],
    });
  });
});
