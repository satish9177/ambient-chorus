import { expect, test } from "@playwright/test";

test("loads the Phase 0 shell", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Ambient CHORUS" })).toBeVisible();
});

