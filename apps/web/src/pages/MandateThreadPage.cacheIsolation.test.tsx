import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePersona, PersonaProvider } from "../context/PersonaContext";
import { mockFetchByRoute } from "../test/mockFetch";
import { MandateThreadPage } from "./MandateThreadPage";

const CASE_ID = "c1111111-1111-1111-1111-111111111111";
const CONTRIBUTOR_A = "aaaaaaaa-1111-1111-1111-111111111111";

function baseThread(overrides: Record<string, unknown> = {}) {
  return {
    mandate_id: "m1111111-1111-1111-1111-111111111111",
    case_id: CASE_ID,
    case_state: "AWAITING_MANDATES",
    contributor_id: CONTRIBUTOR_A,
    current_version: 1,
    status: "PROPOSED",
    terms_hash: "sha256:aa",
    fact_permissions: [
      {
        fact_id: "f1",
        fact_type: "INCIDENT_OCCURRENCE",
        wording: "RESIDENT_A_PRIVATE_ROW",
        policy_maximum_scope: "EXTERNAL_ACTION",
        proposed_scope: "ANONYMOUS_CASE",
        current_scope: "ANONYMOUS_CASE",
        allow_safe_transformation: true,
        requires_identity_grant: false,
        locked_reason: null,
      },
    ],
    identity_permission: {
      externally_shareable: false,
      max_scope: "ANONYMOUS_CASE",
      policy_maximum_scope: "NAMED_CASE",
    },
    allowed_destination_ids: ["property_manager:demo"],
    allowed_purposes: ["REQUEST_ELEVATOR_REPAIR_AND_RESPONSE"],
    valid_from: "2030-01-14T09:00:00.000000Z",
    expires_at: null,
    proposed_at: "2030-01-14T09:00:00.000000Z",
    decided_at: null,
    revoked_at: null,
    history: [],
    ...overrides,
  };
}

function PersonaSwitchButton({ to }: { to: "resident_a" | "resident_b" }) {
  const { setActor } = usePersona();
  return (
    <button type="button" onClick={() => setActor(to)}>
      switch to {to}
    </button>
  );
}

function renderHarness() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider>
        <PersonaSwitchButton to="resident_a" />
        <PersonaSwitchButton to="resident_b" />
        <MemoryRouter initialEntries={[`/mandates/${CONTRIBUTOR_A}?case=${CASE_ID}`]}>
          <Routes>
            <Route path="/mandates/:contributorId" element={<MandateThreadPage />} />
          </Routes>
        </MemoryRouter>
      </PersonaProvider>
    </QueryClientProvider>,
  );
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe("MandateThreadPage cache isolation across persona switches (P1-2)", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "resident_a");
  });

  it("never keeps Resident A's rows on screen once the active persona becomes Resident B, and issues a fresh request", async () => {
    const { calls } = mockFetchByRoute([
      (call) => {
        if (!call.url.includes("/session")) return undefined;
        const actor = call.headers["X-Chorus-Demo-Actor"];
        if (actor === "resident_a") {
          return {
            status: 200,
            body: {
              actor: "resident_a",
              contributor_id: CONTRIBUTOR_A,
              community_id: "comm-1",
              namespace: "DEMO",
              capabilities: ["DECIDE_MANDATE", "VERIFY_COMMITMENT"],
            },
          };
        }
        if (actor === "resident_b") {
          return {
            status: 200,
            body: {
              actor: "resident_b",
              contributor_id: "bbbbbbbb-2222-2222-2222-222222222222",
              community_id: "comm-1",
              namespace: "DEMO",
              capabilities: ["DECIDE_MANDATE", "VERIFY_COMMITMENT"],
            },
          };
        }
        return undefined;
      },
      (call) => {
        if (!call.url.includes("/mandates/current")) return undefined;
        const actor = call.headers["X-Chorus-Demo-Actor"];
        if (actor === "resident_a") {
          return { status: 200, body: baseThread() };
        }
        // Resident B has no standing to read Resident A's thread — the server refuses.
        return {
          status: 403,
          body: {
            type: "urn:chorus:error:forbidden",
            title: "Caller is not permitted",
            status: 403,
            code: "FORBIDDEN",
            detail: "This caller may not use this surface.",
            correlation_id: "corr-1",
            retryable: false,
            errors: [],
          },
        };
      },
    ]);

    renderHarness();

    expect(await screen.findByText("RESIDENT_A_PRIVATE_ROW")).toBeInTheDocument();
    const callsBeforeSwitch = calls.length;

    screen.getByRole("button", { name: "switch to resident_b" }).click();

    // A's private row must disappear — not stay rendered from cache — the instant the active
    // persona is no longer Resident A.
    await waitFor(() => {
      expect(screen.queryByText("RESIDENT_A_PRIVATE_ROW")).not.toBeInTheDocument();
    });

    // A fresh request must actually have been issued under the new persona, not silently
    // reused from the cache entry fetched as Resident A.
    await waitFor(() => {
      expect(calls.length).toBeGreaterThan(callsBeforeSwitch);
    });
    const mandateCallAsB = calls.find(
      (c) => c.url.includes("/mandates/current") && c.headers["X-Chorus-Demo-Actor"] === "resident_b",
    );
    expect(mandateCallAsB).toBeDefined();
  });

  it("shows only the active resident's own row through A -> B -> A switches under network delay", async () => {
    const sessionByActor: Record<string, unknown> = {
      resident_a: {
        actor: "resident_a",
        contributor_id: CONTRIBUTOR_A,
        community_id: "comm-1",
        namespace: "DEMO",
        capabilities: ["DECIDE_MANDATE", "VERIFY_COMMITMENT"],
      },
      resident_b: {
        actor: "resident_b",
        contributor_id: "bbbbbbbb-2222-2222-2222-222222222222",
        community_id: "comm-1",
        namespace: "DEMO",
        capabilities: ["DECIDE_MANDATE", "VERIFY_COMMITMENT"],
      },
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const actor = (init?.headers as Record<string, string> | undefined)?.[
          "X-Chorus-Demo-Actor"
        ];
        if (url.includes("/session")) {
          return new Response(JSON.stringify(sessionByActor[actor ?? ""]), { status: 200 });
        }
        if (url.includes("/mandates/current")) {
          // Every resident_b read is slow; resident_a stays fast, mirroring a real network
          // where the two requests race and either could resolve first.
          if (actor === "resident_b") {
            await delay(30);
            return new Response(
              JSON.stringify({
                type: "urn:chorus:error:forbidden",
                title: "Caller is not permitted",
                status: 403,
                code: "FORBIDDEN",
                detail: "This caller may not use this surface.",
                correlation_id: "corr-2",
                retryable: false,
                errors: [],
              }),
              { status: 403 },
            );
          }
          return new Response(JSON.stringify(baseThread()), { status: 200 });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    renderHarness();
    expect(await screen.findByText("RESIDENT_A_PRIVATE_ROW")).toBeInTheDocument();

    screen.getByRole("button", { name: "switch to resident_b" }).click();
    // While B's (slow) read is still in flight, A's row must already be gone.
    await waitFor(() => {
      expect(screen.queryByText("RESIDENT_A_PRIVATE_ROW")).not.toBeInTheDocument();
    });

    screen.getByRole("button", { name: "switch to resident_a" }).click();
    await waitFor(() => {
      expect(screen.getByText("RESIDENT_A_PRIVATE_ROW")).toBeInTheDocument();
    });

    screen.getByRole("button", { name: "switch to resident_b" }).click();
    await waitFor(() => {
      expect(screen.queryByText("RESIDENT_A_PRIVATE_ROW")).not.toBeInTheDocument();
    });

    // B's slow response finally lands; it must never resurrect A's row.
    await delay(60);
    expect(screen.queryByText("RESIDENT_A_PRIVATE_ROW")).not.toBeInTheDocument();
  });
});
