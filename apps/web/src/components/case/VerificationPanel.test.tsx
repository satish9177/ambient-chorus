import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CommitmentSafe } from "../../api/types";
import { VerificationPanel } from "./VerificationPanel";

function commitment(overrides: Partial<CommitmentSafe>): CommitmentSafe {
  return {
    commitment_id: "commit-1",
    action_id: "act-1",
    obligor: "Property manager",
    action_text: "restore elevator B to service",
    due_at: "2030-01-14T00:00:00.000000Z",
    verification_method: "resident confirmation",
    status: "PENDING",
    schedule_generation: 1,
    version: 1,
    verified_by_contributor_id: null,
    outcome_note: null,
    created_at: "2030-01-13T00:00:00.000000Z",
    updated_at: "2030-01-13T00:00:00.000000Z",
    schedule_status: "CREATED",
    schedule_last_error_code: null,
    ...overrides,
  };
}

function renderPanel(status: CommitmentSafe["status"], actor: "presenter_admin" | "resident_a") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <VerificationPanel caseId="case-1" commitment={commitment({ status })} actor={actor} />
    </QueryClientProvider>,
  );
}

describe("VerificationPanel", () => {
  it("renders nothing when the commitment is not DUE", () => {
    const { container } = renderPanel("PENDING", "resident_a");
    expect(container).toBeEmptyDOMElement();
  });

  it("hides the decision buttons from a non-resident persona", () => {
    renderPanel("DUE", "presenter_admin");
    expect(screen.queryByRole("button", { name: "Fulfilled" })).not.toBeInTheDocument();
    expect(screen.getByText(/switch to the affected resident/i)).toBeInTheDocument();
  });

  it("shows Fulfilled/Missed controls to a resident persona once DUE", () => {
    renderPanel("DUE", "resident_a");
    expect(screen.getByRole("button", { name: "Fulfilled" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Missed" })).toBeInTheDocument();
  });
});
