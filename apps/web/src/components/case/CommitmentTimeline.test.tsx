import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CommitmentSafe } from "../../api/types";
import { CommitmentTimeline } from "./CommitmentTimeline";

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

describe("CommitmentTimeline scheduling display (P2-9)", () => {
  it("shows Scheduled once the schedule projection reports CREATED", () => {
    render(<CommitmentTimeline commitments={[commitment({ schedule_status: "CREATED" })]} />);
    expect(screen.getByText("Scheduled")).toBeInTheDocument();
  });

  it("shows Pending scheduling before the schedule row is created", () => {
    render(
      <CommitmentTimeline
        commitments={[commitment({ schedule_status: "PENDING_SCHEDULE", schedule_last_error_code: null })]}
      />,
    );
    expect(screen.getByText("Pending scheduling")).toBeInTheDocument();
  });

  it("shows a retrying state with the closed failure code, never scheduler internals", () => {
    render(
      <CommitmentTimeline
        commitments={[
          commitment({
            schedule_status: "PENDING_SCHEDULE",
            schedule_last_error_code: "SCHEDULER_UNAVAILABLE",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/retrying/i)).toHaveTextContent("SCHEDULER_UNAVAILABLE");
    expect(screen.queryByText(/scheduler_name/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/due_event/i)).not.toBeInTheDocument();
  });

  it("shows no scheduling badge when the projection is absent", () => {
    render(<CommitmentTimeline commitments={[commitment({ schedule_status: null })]} />);
    expect(screen.queryByText("Scheduled")).not.toBeInTheDocument();
    expect(screen.queryByText("Pending scheduling")).not.toBeInTheDocument();
  });
});
