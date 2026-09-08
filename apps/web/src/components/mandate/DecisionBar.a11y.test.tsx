import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DecisionBar } from "./DecisionBar";

describe("DecisionBar accessibility", () => {
  it("reaches Approve, Adjust, and Refuse by keyboard alone, each with a clear label", async () => {
    const user = userEvent.setup();
    render(
      <DecisionBar
        canDecide
        status="PROPOSED"
        isAdjusting={false}
        pending={false}
        onApprove={vi.fn()}
        onStartAdjust={vi.fn()}
        onSubmitAdjust={vi.fn()}
        onCancelAdjust={vi.fn()}
        onRefuse={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );

    await user.tab();
    expect(screen.getByRole("button", { name: "Approve" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Adjust" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Refuse" })).toHaveFocus();
  });

  it("activates Approve with the Enter key", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    render(
      <DecisionBar
        canDecide
        status="PROPOSED"
        isAdjusting={false}
        pending={false}
        onApprove={onApprove}
        onStartAdjust={vi.fn()}
        onSubmitAdjust={vi.fn()}
        onCancelAdjust={vi.fn()}
        onRefuse={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );

    await user.tab();
    await user.keyboard("{Enter}");
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it("explains, without decision buttons, when the viewer cannot decide", () => {
    render(
      <DecisionBar
        canDecide={false}
        status="PROPOSED"
        isAdjusting={false}
        pending={false}
        onApprove={vi.fn()}
        onStartAdjust={vi.fn()}
        onSubmitAdjust={vi.fn()}
        onCancelAdjust={vi.fn()}
        onRefuse={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/switch to this resident/i)).toBeInTheDocument();
  });
});
