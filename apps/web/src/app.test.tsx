import "@testing-library/jest-dom/vitest";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./app";

describe("App", () => {
  it("renders the Phase 0 foundation", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "Ambient CHORUS" })).toBeInTheDocument();
  });
});

