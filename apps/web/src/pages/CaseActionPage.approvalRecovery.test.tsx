import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PersonaProvider } from "../context/PersonaContext";
import { DemoCaseProvider } from "../context/DemoCaseContext";
import { mockFetchByRoute, type MockedCall } from "../test/mockFetch";
import { CaseActionPage } from "./CaseActionPage";

const CASE_ID = "c3333333-3333-3333-3333-333333333333";
const ACTION_ID = "act-recover-1";
const EXECUTION_ID = "exec-recover-1";
const APPROVAL_ID = "appr-recover-1";

function currentAction(overrides: Record<string, unknown> = {}) {
  return {
    action_id: ACTION_ID,
    status: "ACTION_PROPOSED",
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
    execution: {
      execution_id: EXECUTION_ID,
      state: "APPROVED",
      version: 2,
      approval_id: APPROVAL_ID,
    },
    approval_authorization_current: true,
    ...overrides,
  };
}

function caseSurfaceBody(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  };
}

function renderHarness() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
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

describe("CaseActionPage approval survives a reload (P2-4)", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("chorus.demo.actor", "case_approver");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads the durable approval_id straight from the case surface and executes with it, unchanged", async () => {
    const { calls } = mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/executions")) return undefined;
        return {
          status: 202,
          body: { operation_id: "op-1", status: "PENDING", poll_url: "/v1/operations/op-1" },
        };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/operations/")) return undefined;
        return {
          status: 200,
          body: {
            operation_id: "op-1",
            kind: "SEND_ACTION",
            status: "SUCCEEDED",
            result_refs: [],
            error_code: null,
            created_at: "2030-01-14T09:00:00.000000Z",
            updated_at: "2030-01-14T09:00:00.000000Z",
          },
        };
      },
    ]);

    // Simulates a fresh page load as the approver, with no prior in-memory state from
    // whichever session actually clicked "Approve" — everything comes from this one GET.
    renderHarness();

    const executeButton = await screen.findByRole("button", { name: "Execute / send" });
    await userEvent.click(executeButton);

    await waitFor(() => {
      const executeCall = calls.find((c) => c.url.includes("/executions"));
      expect(executeCall).toBeDefined();
    });

    const executeCall = calls.find((c) => c.url.includes("/executions"))!;
    expect(executeCall.body).toEqual({
      execution_id: EXECUTION_ID,
      expected_execution_version: 2,
      approval_id: APPROVAL_ID,
    });
  });

  it("refetches the case and sends no second execution when the send operation terminally CONFLICTs (P2)", async () => {
    let caseSurfaceReads = 0;
    let executePosts = 0;

    const { calls } = mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        caseSurfaceReads += 1;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/executions")) return undefined;
        executePosts += 1;
        return {
          status: 202,
          body: { operation_id: "op-x", status: "PENDING", poll_url: "/v1/operations/op-x" },
        };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/operations/")) return undefined;
        return {
          status: 200,
          body: {
            operation_id: "op-x",
            kind: "SEND_ACTION",
            status: "FAILED",
            result_refs: [],
            error_code: "CONFLICT",
            created_at: "2030-01-14T09:00:00.000000Z",
            updated_at: "2030-01-14T09:00:00.000000Z",
          },
        };
      },
    ]);

    renderHarness();
    const executeButton = await screen.findByRole("button", { name: "Execute / send" });
    expect(caseSurfaceReads).toBe(1);
    await userEvent.click(executeButton);

    // The terminal CONFLICT triggers exactly one case refetch...
    await waitFor(() => expect(caseSurfaceReads).toBe(2));
    // ...and the execution is never automatically resubmitted.
    await new Promise((r) => setTimeout(r, 20));
    expect(executePosts).toBe(1);
    expect(calls.filter((c) => c.url.includes("/executions"))).toHaveLength(1);
  });

  it("disables Execute for the whole async-conflict recovery refetch and never resubmits the stale version (P2)", async () => {
    let caseFetches = 0;
    let executePosts = 0;
    const execBodies: unknown[] = [];
    let releaseRefetch!: () => void;
    const refetchGate = new Promise<void>((resolve) => {
      releaseRefetch = resolve;
    });

    const surfaceAt = (version: number) =>
      caseSurfaceBody({
        current_action: currentAction({
          execution: {
            execution_id: EXECUTION_ID,
            state: "APPROVED",
            version,
            approval_id: APPROVAL_ID,
          },
        }),
      });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const body = init?.body ? JSON.parse(init.body as string) : undefined;
        const respond = (status: number, payload: unknown) =>
          new Response(JSON.stringify(payload), {
            status,
            headers: { "Content-Type": "application/json" },
          });

        if (/\/cases\/[^/]+$/.test(new URL(url, "http://x").pathname)) {
          caseFetches += 1;
          if (caseFetches === 1) return respond(200, surfaceAt(1));
          // The recovery refetch: held pending until the test releases it, then fresh @ v2.
          await refetchGate;
          return respond(200, surfaceAt(2));
        }
        if (url.includes("/executions")) {
          executePosts += 1;
          execBodies.push(body);
          return respond(202, {
            operation_id: "op-x",
            status: "PENDING",
            poll_url: "/v1/operations/op-x",
          });
        }
        if (url.includes("/operations/")) {
          return respond(200, {
            operation_id: "op-x",
            kind: "SEND_ACTION",
            status: "FAILED",
            result_refs: [],
            error_code: "CONFLICT",
            created_at: "2030-01-14T09:00:00.000000Z",
            updated_at: "2030-01-14T09:00:00.000000Z",
          });
        }
        return respond(404, {});
      }),
    );

    renderHarness();

    // 1-2. APPROVED @ v1, click Execute.
    const executeButton = await screen.findByRole("button", { name: "Execute / send" });
    await userEvent.click(executeButton);
    await waitFor(() => expect(executePosts).toBe(1));

    // 3-5. Operation polls to FAILED/CONFLICT; the refetch is gated (pending). Execute must be
    // disabled immediately and stay disabled for the whole recovery.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Execute / send" })).toBeDisabled(),
    );

    // 6-7. A second click during recovery submits nothing.
    await userEvent.click(screen.getByRole("button", { name: "Execute / send" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(executePosts).toBe(1);
    expect(screen.getByRole("button", { name: "Execute / send" })).toBeDisabled();

    // 8-9. Fresh state (v2) arrives; the button renders from the new projection and re-enables.
    releaseRefetch();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Execute / send" })).toBeEnabled(),
    );

    // 10-11. A fresh deliberate click sends the *new* version.
    await userEvent.click(screen.getByRole("button", { name: "Execute / send" }));
    await waitFor(() => expect(executePosts).toBe(2));
    expect(execBodies).toHaveLength(2);
    expect(execBodies[1]).toEqual({
      execution_id: EXECUTION_ID,
      expected_execution_version: 2,
      approval_id: APPROVAL_ID,
    });
    // The first attempt carried the stale version; it was never repeated during recovery.
    expect(execBodies[0]).toEqual({
      execution_id: EXECUTION_ID,
      expected_execution_version: 1,
      approval_id: APPROVAL_ID,
    });
  });

  it("an ordinary non-conflict operation failure does not enter stale-recovery mode", async () => {
    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        return { status: 200, body: caseSurfaceBody() };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/executions")) return undefined;
        return {
          status: 202,
          body: { operation_id: "op-y", status: "PENDING", poll_url: "/v1/operations/op-y" },
        };
      },
      (call: MockedCall) => {
        if (!call.url.includes("/operations/")) return undefined;
        return {
          status: 200,
          body: {
            operation_id: "op-y",
            kind: "SEND_ACTION",
            status: "FAILED",
            result_refs: [],
            error_code: "INTERNAL_ERROR",
            created_at: "2030-01-14T09:00:00.000000Z",
            updated_at: "2030-01-14T09:00:00.000000Z",
          },
        };
      },
    ]);

    renderHarness();
    const executeButton = await screen.findByRole("button", { name: "Execute / send" });
    await userEvent.click(executeButton);

    // The plain failure is shown by OperationProgress; the button is not stuck disabled by a
    // recovery hold — it settles back to enabled once the (non-gated) refetch completes.
    await screen.findByText(/Send failed \(INTERNAL_ERROR\)\./i);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Execute / send" })).toBeEnabled(),
    );
  });

  it("never offers Execute for a rejected/invalidated execution even if approval_id was once set", async () => {
    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        return {
          status: 200,
          body: caseSurfaceBody({
            current_action: currentAction({
              execution: {
                execution_id: EXECUTION_ID,
                state: "FAILED",
                version: 3,
                approval_id: APPROVAL_ID,
              },
            }),
          }),
        };
      },
    ]);

    renderHarness();
    await screen.findByText("Send failed.");
    expect(screen.queryByRole("button", { name: "Execute / send" })).not.toBeInTheDocument();
  });

  it("withdraws Execute after a mandate revocation invalidates the durable approval (P2)", async () => {
    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        return {
          status: 200,
          body: caseSurfaceBody({
            current_action: currentAction({ approval_authorization_current: false }),
          }),
        };
      },
    ]);

    renderHarness();
    await screen.findByText(/Approval is no longer valid because disclosure authorization changed/i);
    expect(screen.queryByRole("button", { name: "Execute / send" })).not.toBeInTheDocument();
  });

  it("still offers Execute on a fresh reload when authorization has not changed (P2 regression)", async () => {
    mockFetchByRoute([
      (call: MockedCall) => {
        if (!/\/cases\/[^/]+$/.test(new URL(call.url, "http://x").pathname)) return undefined;
        return {
          status: 200,
          body: caseSurfaceBody({
            current_action: currentAction({ approval_authorization_current: true }),
          }),
        };
      },
    ]);

    renderHarness();
    expect(await screen.findByRole("button", { name: "Execute / send" })).toBeInTheDocument();
  });
});
