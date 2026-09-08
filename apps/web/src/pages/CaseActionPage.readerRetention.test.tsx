import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DemoCaseProvider } from "../context/DemoCaseContext";
import { PersonaProvider, usePersona } from "../context/PersonaContext";
import { mockFetchByRoute, type MockedCall, type RouteResponder } from "../test/mockFetch";
import { CaseActionPage } from "./CaseActionPage";

const CASE_ID = "c5555555-5555-5555-5555-555555555555";
const PRESENTER_TITLE = "Recurring lift failures";

function caseSurfaceBody(title: string | null) {
  return {
    case_id: CASE_ID,
    case: {
      case_id: CASE_ID,
      title,
      state: "AWAITING_MANDATES",
      version: 2,
      authorization_version: 1,
      issue_type: "ELEVATOR_FAILURE",
      corroboration_source_count: 0,
      state_reason_code: "MONITOR_CANDIDATE_DETECTED",
    },
    evidence_summary: title === null ? null : [],
    current_shareable_view: null,
    current_action: null,
    commitments: [],
    privacy_counts: null,
  };
}

const isCasePath = (url: string) => /\/cases\/[^/]+$/.test(new URL(url, "http://x").pathname);

/** The backend serves every persona the safe subset; only the presenter gets the private title. */
const caseSurfaceResponder: RouteResponder = (call) => {
  if (!isCasePath(call.url)) return undefined;
  const presenter = call.headers["X-Chorus-Demo-Actor"] === "presenter_admin";
  return { status: 200, body: caseSurfaceBody(presenter ? PRESENTER_TITLE : null) };
};

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
  render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider>
        <DemoCaseProvider>
          <PersonaSwitchButton to="presenter_admin" />
          <PersonaSwitchButton to="resident_a" />
          <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
            <Link to="/mandates/some-contributor">Go to mandate thread</Link>
            <Link to={`/cases/${CASE_ID}`}>Return to case</Link>
            <Routes>
              <Route path="/cases/:caseId" element={<CaseActionPage />} />
              <Route path="/mandates/:contributorId" element={<p>Mandate thread page</p>} />
            </Routes>
          </MemoryRouter>
        </DemoCaseProvider>
      </PersonaProvider>
    </QueryClientProvider>,
  );
}

const caseCalls = (calls: MockedCall[]) => calls.filter((c) => isCasePath(c.url));
const actorOf = (call: MockedCall | undefined) => call?.headers["X-Chorus-Demo-Actor"];
/** A resident's safe subset carries no private title, so the heading falls back to "Case". */
const residentHeading = () => screen.findByRole("heading", { level: 2, name: "Case" });

describe("CaseActionPage reads the case as the currently active persona (P1-1)", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "presenter_admin");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("A: the presenter's case read carries the presenter header", async () => {
    const { calls } = mockFetchByRoute([caseSurfaceResponder]);
    renderHarness();

    await screen.findByText(PRESENTER_TITLE);
    const reads = caseCalls(calls);
    expect(reads).toHaveLength(1);
    expect(actorOf(reads[0])).toBe("presenter_admin");
  });

  it("B: switching to a resident makes the next case read as that resident", async () => {
    const { calls } = mockFetchByRoute([caseSurfaceResponder]);
    renderHarness();

    await screen.findByText(PRESENTER_TITLE);
    screen.getByRole("button", { name: "switch to resident_a" }).click();

    await residentHeading();
    expect(actorOf(caseCalls(calls).at(-1))).toBe("resident_a");
    // The presenter's private title is not rendered as resident-visible state.
    expect(screen.queryByText(PRESENTER_TITLE)).not.toBeInTheDocument();
  });

  it("C: the presenter projection is dropped the instant the persona changes", async () => {
    const { calls } = mockFetchByRoute([caseSurfaceResponder]);
    renderHarness();

    await screen.findByText(PRESENTER_TITLE);
    screen.getByRole("button", { name: "switch to resident_a" }).click();

    // A different query key: the resident entry has no cached data, so the page shows its own
    // loading / safe-subset state — never the presenter projection it just had on screen.
    await waitFor(() => {
      expect(screen.queryByText(PRESENTER_TITLE)).not.toBeInTheDocument();
    });
    await residentHeading();
    expect(actorOf(caseCalls(calls).at(-1))).toBe("resident_a");
  });

  it("D: navigating to a mandate thread and back keeps the resident identity", async () => {
    const { calls } = mockFetchByRoute([caseSurfaceResponder]);
    renderHarness();

    await screen.findByText(PRESENTER_TITLE);
    screen.getByRole("button", { name: "switch to resident_a" }).click();
    await residentHeading();

    screen.getByRole("link", { name: "Go to mandate thread" }).click();
    await screen.findByText("Mandate thread page");
    screen.getByRole("link", { name: "Return to case" }).click();

    await residentHeading();
    // Every case read after the initial presenter one was made as the resident.
    for (const call of caseCalls(calls).slice(1)) {
      expect(call.headers["X-Chorus-Demo-Actor"]).toBe("resident_a");
    }
  });

  it("E: switching back to the presenter issues a fresh presenter read", async () => {
    const { calls } = mockFetchByRoute([caseSurfaceResponder]);
    renderHarness();

    await screen.findByText(PRESENTER_TITLE);
    screen.getByRole("button", { name: "switch to resident_a" }).click();
    await residentHeading();

    screen.getByRole("button", { name: "switch to presenter_admin" }).click();
    await screen.findByText(PRESENTER_TITLE);

    const reads = caseCalls(calls);
    expect(actorOf(reads.at(-1))).toBe("presenter_admin");
    expect(reads.length).toBeGreaterThanOrEqual(3);
  });

  it("F: a delayed presenter response cannot overwrite a newer resident state", async () => {
    const calls: MockedCall[] = [];
    let releasePresenter!: () => void;
    const presenterGate = new Promise<void>((resolve) => {
      releasePresenter = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const headers = (init?.headers as Record<string, string> | undefined) ?? {};
        calls.push({ url, method: init?.method ?? "GET", headers, body: undefined });
        const respond = (body: unknown) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        if (!isCasePath(url)) return new Response("{}", { status: 404 });
        if (headers["X-Chorus-Demo-Actor"] === "presenter_admin") {
          await presenterGate; // stall until the persona has already switched away
          return respond(caseSurfaceBody(PRESENTER_TITLE));
        }
        return respond(caseSurfaceBody(null));
      }),
    );

    renderHarness();
    // The presenter read is in flight (stalled). Switch to the resident before it lands.
    screen.getByRole("button", { name: "switch to resident_a" }).click();
    await residentHeading();

    releasePresenter();
    await new Promise((r) => setTimeout(r, 20));

    // The stalled presenter response resolved into the presenter query key, which nothing is
    // observing — the resident's safe-subset state is untouched.
    await residentHeading();
    expect(screen.queryByText(PRESENTER_TITLE)).not.toBeInTheDocument();
    expect(caseCalls(calls).some((c) => c.headers["X-Chorus-Demo-Actor"] === "resident_a")).toBe(
      true,
    );
  });
});
