import { vi } from "vitest";

export type MockedCall = {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
};

/** Queues one JSON response per call, in order, and records every request made. */
export function mockFetchSequence(
  responses: { status: number; body: unknown }[],
): { calls: MockedCall[] } {
  const calls: MockedCall[] = [];
  let index = 0;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      // Read the plain header object the client actually builds, rather than routing it
      // through the `Headers` class first — that would lowercase every name, and a test
      // asserting the literal `Idempotency-Key` casing the API expects deserves to see it.
      const headers = (init?.headers as Record<string, string> | undefined) ?? {};
      const body = init?.body ? JSON.parse(init.body as string) : undefined;
      calls.push({ url, method: init?.method ?? "GET", headers, body });

      if (responses.length === 0) throw new Error("mockFetchSequence: no responses configured");
      const next = responses[Math.min(index, responses.length - 1)]!;
      index += 1;
      return new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json", "X-Correlation-Id": crypto.randomUUID() },
      });
    }),
  );

  return { calls };
}

export type RouteResponder = (call: MockedCall) => { status: number; body: unknown } | undefined;

/**
 * Resolves a response by inspecting each request (URL, method, actor header, body) rather than
 * strict call order — needed wherever two queries can legitimately fire in either order (e.g.
 * two `useQuery`s that both depend on a persona that just changed) and a positional sequence
 * would be flaky.
 */
export function mockFetchByRoute(
  responders: RouteResponder[],
): { calls: MockedCall[] } {
  const calls: MockedCall[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const headers = (init?.headers as Record<string, string> | undefined) ?? {};
      const body = init?.body ? JSON.parse(init.body as string) : undefined;
      const call: MockedCall = { url, method: init?.method ?? "GET", headers, body };
      calls.push(call);

      for (const responder of responders) {
        const match = responder(call);
        if (match) {
          return new Response(JSON.stringify(match.body), {
            status: match.status,
            headers: {
              "Content-Type": "application/json",
              "X-Correlation-Id": crypto.randomUUID(),
            },
          });
        }
      }
      throw new Error(`mockFetchByRoute: no responder matched ${call.method} ${call.url}`);
    }),
  );

  return { calls };
}
