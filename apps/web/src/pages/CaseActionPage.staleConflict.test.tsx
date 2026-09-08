import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { DemoCaseProvider } from "../context/DemoCaseContext";
import { PersonaProvider } from "../context/PersonaContext";
import { mockFetchByRoute, type MockedCall } from "../test/mockFetch";
import { CaseActionPage } from "./CaseActionPage";

const CASE_ID = "c4444444-4444-4444-4444-444444444444";

function currentAction() {
  return {
    action_id: "act-1",
    status: "PROPOSED",
    view_id: "view-1",
    view_hash: "sha256:" + "a".repeat(64),
    case_version: 5,
    authorization_version: 1,
    subject: "Elevator B outage",
    claims: [{ text: "Elevator B has failed six times.", export_fact_ids: ["e1"] }],
    requested_action: "Please repair elevator B.",
    caveats: [],
    tone: "NEUTRAL",
    proposal_hash: "sha256:" + "b".repeat(64),
    preview: {
      template_version: "action-email/v1",
      text_body: "Please repair elevator B.",
      html_body: "<p>Please repair elevator B.</p>",
      preview_hash: "sha256:" + "c".repeat(64),
      matches_committed_hash: true,
    },
    execution: { execution_id: "exec-1", state: "DRAFT", version: 1, approval_id: null },
    approval_authorization_current: true,
  };
}

function caseSurfaceBody() {
  return {
    case_id: CASE_ID,
    case: {
      case_id: CASE_ID,
      title: "Recurring lift failures",
      state: "ACTION_PROPOSED",
      version: 5,
      authorization_version: 1,
      issue_type: "ELEVATOR_FAILURE",
      corroboration_source_count: 2,
      state_reason_code: "ACTION_PROPOSED",
    },
    evidence_summary: [],
    current_shareable_view: null,
    current_action: currentAction(),
    commitments: [],
    privacy_counts: null,
  };
}

function renderHarness() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <PersonaProvider>
        <DemoCaseProvider>
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

describe("CaseActionPage fails closed on a stale approval conflict (P2-5)", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "case_approver");
  });

  it("refetches the case surface and never resubmits the same approval body automatically", async () => {
    let caseSurfaceReads = 0;
    let approvalAttempts = 0;

    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        caseSurfaceReads += 1;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/approvals")) return undefined;
        approvalAttempts += 1;
        return {
          status: 409,
          body: {
            type: "urn:chorus:error:stale-authorization",
            title: "Concurrent modification",
            status: 409,
            code: "PERSISTENCE_CONFLICT",
            detail: "Reload the current version and retry the command.",
            correlation_id: "corr-stale",
            retryable: false,
            errors: [],
          },
        };
      },
    ]);

    renderHarness();

    const approveButton = await screen.findByRole("button", { name: "Approve" });
    expect(caseSurfaceReads).toBe(1);

    await userEvent.click(approveButton);

    // The case surface must be refetched once the conflict lands...
    await waitFor(() => expect(caseSurfaceReads).toBe(2));

    // ...and the approval itself must never be silently resubmitted with the same stale body.
    expect(approvalAttempts).toBe(1);
  });

  it("treats a 422 VALIDATION_ERROR carrying a stale-binding reason code as a conflict", async () => {
    let caseSurfaceReads = 0;
    let approvalAttempts = 0;

    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        caseSurfaceReads += 1;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/approvals")) return undefined;
        approvalAttempts += 1;
        return {
          status: 422,
          body: {
            type: "urn:chorus:error:validation-error",
            title: "Request is not valid",
            status: 422,
            code: "VALIDATION_ERROR",
            detail: "The request did not satisfy the contract for this endpoint.",
            correlation_id: "corr-422-stale",
            retryable: false,
            // The additive structured signal from the domain problem-details handler.
            errors: ["EXECUTION_VERSION_MISMATCH"],
          },
        };
      },
    ]);

    renderHarness();
    const approveButton = await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(approveButton);

    await waitFor(() => expect(caseSurfaceReads).toBe(2));
    expect(approvalAttempts).toBe(1);
  });

  it("does NOT treat an ordinary 422 validation error as a stale conflict", async () => {
    let caseSurfaceReads = 0;
    let approvalAttempts = 0;

    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        caseSurfaceReads += 1;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/approvals")) return undefined;
        approvalAttempts += 1;
        return {
          status: 422,
          body: {
            type: "urn:chorus:error:validation-error",
            title: "Request is not valid",
            status: 422,
            code: "VALIDATION_ERROR",
            detail: "The request did not satisfy the contract for this endpoint.",
            correlation_id: "corr-422-plain",
            retryable: false,
            errors: [{ code: "MISSING", path: "body.decision", category: "MISSING" }],
          },
        };
      },
    ]);

    renderHarness();
    const approveButton = await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(approveButton);

    // The mutation error surfaces, but the case is NOT refetched as if the view were stale.
    await screen.findByRole("alert");
    expect(approvalAttempts).toBe(1);
    expect(caseSurfaceReads).toBe(1);
  });
});
