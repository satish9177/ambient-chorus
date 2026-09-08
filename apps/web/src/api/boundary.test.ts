import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

function exportedTypeNames(relativePath: string): Set<string> {
  const path = fileURLToPath(new URL(relativePath, import.meta.url));
  const source = readFileSync(path, "utf-8");
  const names = new Set<string>();
  for (const match of source.matchAll(/export type (\w+)/g)) {
    const name = match[1];
    if (name) names.add(name);
  }
  return names;
}

describe("private/shareable type boundary", () => {
  it("exports disjoint type names from private.ts and shareable.ts", () => {
    const privateNames = exportedTypeNames("./private.ts");
    const shareableNames = exportedTypeNames("./shareable.ts");

    const overlap = [...privateNames].filter((name) => shareableNames.has(name));

    expect(overlap).toEqual([]);
    expect(privateNames.size).toBeGreaterThan(0);
    expect(shareableNames.size).toBeGreaterThan(0);
  });
});
