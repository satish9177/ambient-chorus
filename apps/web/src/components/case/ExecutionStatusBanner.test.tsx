import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SafeExecution } from "../../api/shareable";
import { ExecutionStatusBanner } from "./ExecutionStatusBanner";

function renderBanner(
  state: string,
  approvalId: string | null = null,
  approvalAuthorizationCurrent = true,
) {
  const execution: SafeExecution = {
    execution_id: "exec-1",
    state,
    version: 2,
    approval_id: approvalId,
  };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ExecutionStatusBanner
        caseId="case-1"
        actionId="act-1"
        execution={execution}
        approvalAuthorizationCurrent={approvalAuthorizationCurrent}
        isApprover
        actor="case_approver"
      />
    </QueryClientProvider>,
  );
}

describe("ExecutionStatusBanner", () => {
  it("SEND_UNKNOWN removes every send/retry control and explains the ambiguity", () => {
    renderBanner("SEND_UNKNOWN");
    expect(screen.getByRole("alert")).toHaveTextContent(/unknown/i);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/no retry/i)).toBeInTheDocument();
  });

  it("FAILED explains the recovery path without offering a retry on this execution", () => {
    renderBanner("FAILED");
    expect(screen.getByRole("alert")).toHaveTextContent(/failed/i);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/fresh action/i)).toBeInTheDocument();
  });

  it("SENT is visibly distinguished from resolved", () => {
    renderBanner("SENT");
    expect(screen.getByRole("status")).toHaveTextContent(/sent/i);
    expect(screen.getByText(/does not mean the case is resolved/i)).toBeInTheDocument();
  });

  it("APPROVED with no approval_id offers no Execute control (P2-4: nothing to bind to)", () => {
    renderBanner("APPROVED", null);
    expect(screen.queryByRole("button", { name: "Execute / send" })).not.toBeInTheDocument();
  });

  it("APPROVED with a server-provided approval_id offers Execute, surviving a fresh read", () => {
    renderBanner("APPROVED", "appr-recovered-1");
    expect(screen.getByRole("button", { name: "Execute / send" })).toBeInTheDocument();
  });

  it("APPROVED whose approval is no longer authorized withdraws Execute and explains why (P2)", () => {
    renderBanner("APPROVED", "appr-recovered-1", false);
    expect(screen.queryByRole("button", { name: "Execute / send" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Approval is no longer valid because disclosure authorization changed/i,
    );
  });
});
