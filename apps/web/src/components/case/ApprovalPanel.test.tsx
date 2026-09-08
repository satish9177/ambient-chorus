import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { SafeCurrentAction } from "../../api/shareable";
import { mockFetchSequence } from "../../test/mockFetch";
import { ApprovalPanel } from "./ApprovalPanel";

const action: SafeCurrentAction = {
  action_id: "act-1",
  status: "PROPOSED",
  view_id: "view-1",
  view_hash: "sha256:" + "a".repeat(64),
  case_version: 4,
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

function renderPanel(action_: SafeCurrentAction = action) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ApprovalPanel caseId="case-1" action={action_} actor="case_approver" isApprover />
    </QueryClientProvider>,
  );
}

describe("ApprovalPanel", () => {
  it("submits the exact server-returned version/hashes unchanged, never a derived value", async () => {
    const { calls } = mockFetchSequence([
      {
        status: 200,
        body: {
          approval_id: "appr-1",
          decision: "APPROVED",
          approval_hash: "sha256:" + "d".repeat(64),
          expires_at: "2030-01-15T00:00:00.000000Z",
          execution_id: "exec-1",
          execution_state: "APPROVED",
          execution_version: 2,
          pointer_status: "DRAFT",
          case_state: "ACTION_PROPOSED",
          case_version: 5,
          authorization_version: 1,
          replayed: false,
        },
      },
    ]);

    renderPanel();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]!.body).toEqual({
      decision: "APPROVED",
      expected_execution_version: 1,
      execution_id: "exec-1",
      view_hash: action.view_hash,
      proposal_hash: action.proposal_hash,
      preview_hash: action.preview.preview_hash,
    });
    // P2-4: the approval's return payload (approval_id/execution_version/...) is no longer
    // threaded through a callback — the next case-surface read carries `execution.approval_id`
    // itself, which is what makes reload recovery possible. See
    // ExecutionStatusBanner.test.tsx for the read side of that contract.
  });

  it("disables approval and warns when the preview no longer matches the committed hash", () => {
    const staleAction: SafeCurrentAction = {
      ...action,
      preview: { ...action.preview, matches_committed_hash: false },
    };
    renderPanel(staleAction);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });
});
