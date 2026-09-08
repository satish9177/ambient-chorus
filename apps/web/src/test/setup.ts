import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

// No test may reach the real network. Individual tests install their own responses via
// `mockFetchSequence`/`mockFetchOnce` from `./mockFetch`; anything left unmocked fails loudly
// instead of hanging on a real connection attempt.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("no fetch mock was installed for this request"))),
  );
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});
