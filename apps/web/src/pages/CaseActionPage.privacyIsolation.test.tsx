import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { DemoCaseProvider } from "../context/DemoCaseContext";
import { PersonaProvider, usePersona } from "../context/PersonaContext";
import { mockFetchByRoute, type MockedCall } from "../test/mockFetch";
import { CaseActionPage } from "./CaseActionPage";

const CASE_ID = "c2222222-2222-2222-2222-222222222222";
const PRIVATE_SENTINEL = "PRIVATE_HEALTH_DETAIL_ROW";

function caseSurfaceBody(overrides: Record<string, unknown> = {}) {
  return {
    case_id: CASE_ID,
    case: {
      case_id: CASE_ID,
      title: "Recurring lift failures",
      state: "INVESTIGATING",
      version: 3,
      authorization_version: 1,
      issue_type: "ELEVATOR_FAILURE",
      corroboration_source_count: 2,
      state_reason_code: "EVIDENCE_SUFFICIENT",
    },
    evidence_summary: [],
    current_shareable_view: null,
    current_action: null,
    commitments: [],
    privacy_counts: null,
    ...overrides,
  };
}

function investigationBody() {
  return {
    case: {
      case_id: CASE_ID,
      title: "Recurring lift failures",
      state: "INVESTIGATING",
      version: 3,
      authorization_version: 1,
      corroboration_source_count: 2,
    },
    reports: [],
    facts: [
      {
        fact_id: "f1",
        fact_type: "HEALTH_DETAIL",
        sensitivity: "SENSITIVE",
        value_preview: PRIVATE_SENTINEL,
        evidence_status: "REPORTED",
        status: "ACTIVE",
        contributor_id: "ct1",
        evidence_ids: [],
        source_message_ids: [],
        version: 1,
      },
    ],
    assessment: null,
    compile: null,
  };
}

function auditBody() {
  return { items: [], next_cursor: null };
}

function PersonaSwitchButton({ to }: { to: string }) {
  const { setActor } = usePersona();
  return (
    <button type="button" onClick={() => setActor(to as never)}>
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
        <DemoCaseProvider>
          <PersonaSwitchButton to="presenter_admin" />
          <PersonaSwitchButton to="resident_a" />
          <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
            <Routes>
              <Route path="/cases/:caseId" element={<CaseActionPage />} />
            </Routes>
          </MemoryRouter>
        </DemoCaseProvider>
      </PersonaProvider>
    </QueryClientProvider>,
  );
}

function commonResponders(): ((call: MockedCall) => { status: number; body: unknown } | undefined)[] {
  return [
    (call) => {
      if (!call.url.includes(`/cases/${CASE_ID}/investigation`)) return undefined;
      const actor = call.headers["X-Chorus-Demo-Actor"];
      if (actor !== "presenter_admin") {
        return {
          status: 403,
          body: {
            type: "urn:chorus:error:forbidden",
            title: "Caller is not permitted",
            status: 403,
            code: "FORBIDDEN",
            detail: "This caller may not use this surface.",
            correlation_id: "corr-inv",
            retryable: false,
            errors: [],
          },
        };
      }
      return { status: 200, body: investigationBody() };
    },
    (call) => {
      if (!call.url.includes(`/cases/${CASE_ID}/audit`)) return undefined;
      const actor = call.headers["X-Chorus-Demo-Actor"];
      if (actor !== "presenter_admin") {
        return {
          status: 403,
          body: {
            type: "urn:chorus:error:forbidden",
            title: "Caller is not permitted",
            status: 403,
            code: "FORBIDDEN",
            detail: "This caller may not use this surface.",
            correlation_id: "corr-audit",
            retryable: false,
            errors: [],
          },
        };
      }
      return { status: 200, body: auditBody() };
    },
    (call) => {
      // The bare case surface path — must not match the investigation/audit sub-paths above,
      // which this ordering (checked first) already guarantees. It is read as the *live*
      // persona (P1-1). The backend serves every persona the safe subset; only the presenter
      // gets the private title. Nothing here is a presenter projection replayed through a
      // retained identity — the private material lives on the investigation/audit surfaces,
      // which stay presenter-only.
      if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
      const presenter = call.headers["X-Chorus-Demo-Actor"] === "presenter_admin";
      return {
        status: 200,
        body: presenter
          ? caseSurfaceBody()
          : caseSurfaceBody({
              case: { ...caseSurfaceBody().case, title: null },
            }),
      };
    },
  ];
}

describe("CaseActionPage private-read isolation by live persona (P1-1)", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "presenter_admin");
  });

  it("A: presenter persona requests and shows investigation/audit", async () => {
    const { calls } = mockFetchByRoute(commonResponders());
    renderHarness();

    expect(await screen.findByText(PRIVATE_SENTINEL)).toBeInTheDocument();
    const investigationCall = calls.find((c) => c.url.includes("/investigation"));
    expect(investigationCall?.headers["X-Chorus-Demo-Actor"]).toBe("presenter_admin");
  });

  it("B: resident persona reads the safe case subset as itself and issues no investigation/audit request", async () => {
    window.sessionStorage.setItem("chorus.demo.actor", "resident_a");
    const { calls } = mockFetchByRoute(commonResponders());
    renderHarness();

    // The base case read is made — as the resident — and returns the safe subset (no private
    // title, so the heading falls back to "Case"). The private investigation panel is not
    // populated for a resident.
    await screen.findByText(/private investigation is presenter-only/i);

    const caseCall = calls.find((c) =>
      /\/cases\/[^/]+$/.test(new URL(c.url, "http://x").pathname),
    );
    expect(caseCall?.headers["X-Chorus-Demo-Actor"]).toBe("resident_a");
    expect(screen.queryByText(PRIVATE_SENTINEL)).not.toBeInTheDocument();

    // Investigation and audit are gated on the live persona being the presenter, so a resident
    // never triggers them at all — the isolation the elevated read used to bypass.
    expect(calls.find((c) => c.url.includes("/investigation"))).toBeUndefined();
    expect(calls.find((c) => c.url.includes("/audit"))).toBeUndefined();
  });

  it("C: switching from presenter to resident unmounts the private panel immediately", async () => {
    mockFetchByRoute(commonResponders());
    renderHarness();

    expect(await screen.findByText(PRIVATE_SENTINEL)).toBeInTheDocument();

    screen.getByRole("button", { name: "switch to resident_a" }).click();

    await waitFor(() => {
      expect(screen.queryByText(PRIVATE_SENTINEL)).not.toBeInTheDocument();
    });
    // A different query key: the resident's own safe read replaces the presenter surface, and
    // the private panel is gone — not hidden by CSS, unmounted.
    expect(await screen.findByText(/private investigation is presenter-only/i)).toBeInTheDocument();
    expect(screen.queryByText(PRIVATE_SENTINEL)).not.toBeInTheDocument();
  });

  it("D: a cold reload as a resident fetches only the safe case read, and shows nothing private", async () => {
    window.sessionStorage.setItem("chorus.demo.actor", "resident_a");
    const { calls } = mockFetchByRoute(commonResponders());
    renderHarness();

    await screen.findByText(/private investigation is presenter-only/i);
    expect(screen.queryByText(PRIVATE_SENTINEL)).not.toBeInTheDocument();

    // Exactly one request: the base case read, carrying the truthful resident header. There is
    // no retained privileged identity to issue anything on the resident's behalf, and the
    // presenter-only investigation/audit reads are never attempted.
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers["X-Chorus-Demo-Actor"]).toBe("resident_a");
    expect(/\/cases\/[^/]+$/.test(new URL(calls[0]!.url, "http://x").pathname)).toBe(true);
  });

  it("E: every investigation/audit request carries the live actor header, never a retained privileged one", async () => {
    const { calls } = mockFetchByRoute(commonResponders());
    renderHarness();
    await screen.findByText(PRIVATE_SENTINEL);

    screen.getByRole("button", { name: "switch to resident_a" }).click();
    await waitFor(() => {
      expect(screen.queryByText(PRIVATE_SENTINEL)).not.toBeInTheDocument();
    });

    // No investigation/audit call was ever sent with anything but the truthful, live actor —
    // there is no code path that could substitute a stronger header than the one the active
    // persona actually is, so the server's own `require_presenter` check is the only thing
    // standing between a resident and this data, and it was never even asked.
    for (const call of calls) {
      if (call.url.includes("/investigation") || call.url.includes("/audit")) {
        expect(call.headers["X-Chorus-Demo-Actor"]).toBe("presenter_admin");
      }
    }
  });
});
