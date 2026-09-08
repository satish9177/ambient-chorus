import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { PersonaProvider } from "../context/PersonaContext";
import { mockFetchSequence } from "../test/mockFetch";
import { MandateThreadPage } from "./MandateThreadPage";

const CASE_ID = "c1111111-1111-1111-1111-111111111111";
const CONTRIBUTOR_ID = "4b112227-4176-5cfb-bb9a-370d96f4a73a";
const MANDATE_ID = "m1111111-1111-1111-1111-111111111111";

const sessionResponse = {
  actor: "resident_b",
  contributor_id: CONTRIBUTOR_ID,
  community_id: "comm-1",
  namespace: "DEMO",
  capabilities: ["DECIDE_MANDATE", "VERIFY_COMMITMENT"],
};

const threadResponse = {
  mandate_id: MANDATE_ID,
  case_id: CASE_ID,
  case_state: "AWAITING_MANDATES",
  contributor_id: CONTRIBUTOR_ID,
  current_version: 1,
  status: "PROPOSED",
  terms_hash: "sha256:aa",
  fact_permissions: [
    {
      fact_id: "f1",
      fact_type: "INCIDENT_OCCURRENCE",
      wording: "An elevator incident you reported.",
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
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider>
        <MemoryRouter
          initialEntries={[`/mandates/${CONTRIBUTOR_ID}?case=${CASE_ID}`]}
        >
          <Routes>
            <Route path="/mandates/:contributorId" element={<MandateThreadPage />} />
          </Routes>
        </MemoryRouter>
      </PersonaProvider>
    </QueryClientProvider>,
  );
}

describe("MandateThreadPage decisions", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "resident_b");
  });

  it("submits APPROVE with the exact proposed fact_grants and identity_grant, unchanged", async () => {
    const { calls } = mockFetchSequence([
      { status: 200, body: sessionResponse },
      { status: 200, body: threadResponse },
      { status: 200, body: { ...threadResponse, status: "APPROVED", current_version: 2 } },
    ]);

    renderPage();

    const approveButton = await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(approveButton);

    await waitFor(() => {
      const decisionCall = calls.find((c) => c.url.includes("/decisions"));
      expect(decisionCall).toBeDefined();
    });

    const decisionCall = calls.find((c) => c.url.includes("/decisions"))!;
    expect(decisionCall.headers["Idempotency-Key"]).toBeTruthy();
    expect(decisionCall.body).toEqual({
      expected_version: 1,
      decision: "APPROVE",
      fact_grants: [{ fact_id: "f1", max_scope: "ANONYMOUS_CASE", allow_safe_transformation: true }],
      identity_grant: { externally_shareable: false, max_scope: "ANONYMOUS_CASE" },
    });
  });

  it("submits ADJUST with the full replacement grant set the resident chose", async () => {
    const { calls } = mockFetchSequence([
      { status: 200, body: sessionResponse },
      { status: 200, body: threadResponse },
      { status: 200, body: { ...threadResponse, status: "ADJUSTED", current_version: 2 } },
    ]);

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Adjust" }));
    const scopeSelect = await screen.findByLabelText("Content scope");
    await userEvent.selectOptions(scopeSelect, "INTERNAL_ONLY");
    await userEvent.click(screen.getByRole("button", { name: "Submit adjustment" }));

    await waitFor(() => {
      const decisionCall = calls.find((c) => c.url.includes("/decisions"));
      expect(decisionCall).toBeDefined();
    });

    const decisionCall = calls.find((c) => c.url.includes("/decisions"))!;
    expect(decisionCall.body).toEqual({
      expected_version: 1,
      decision: "ADJUST",
      fact_grants: [{ fact_id: "f1", max_scope: "INTERNAL_ONLY", allow_safe_transformation: true }],
      identity_grant: { externally_shareable: false, max_scope: "ANONYMOUS_CASE" },
    });
  });
});
