import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./app";

describe("App", () => {
  it("renders the ambient feed shell", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Ambient CHORUS" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ambient signal feed" })).toBeInTheDocument();
  });
});
