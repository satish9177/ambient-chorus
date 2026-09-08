import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Backs up `no-restricted-imports` in `eslint.config.js` with a test-suite-visible assertion:
 * nothing under `components/shareable/**` may import `api/private`, so a shareable-zone
 * component can never receive a private wire type to begin with.
 */
describe("components/shareable import boundary", () => {
  it("never imports api/private", () => {
    const dir = dirname(fileURLToPath(import.meta.url));
    const files = readdirSync(dir).filter(
      (f: string) => /\.(ts|tsx)$/.test(f) && !f.endsWith(".test.ts"),
    );
    expect(files.length).toBeGreaterThan(0);

    for (const file of files) {
      const source = readFileSync(join(dir, file), "utf-8");
      expect(source).not.toMatch(/from\s+["'].*api\/private["']/);
    }
  });
});
