import { expect, test, type Page } from "@playwright/test";

/**
 * The full five-minute hero flow, driven entirely through in-app navigation and controls
 * against the local API (11-frontend-and-demo.md § Local hero smoke does the backend-only
 * version of this; this is its UI counterpart). Requires `uv run chorus-api serve --port 8080`
 * running separately — `vite.config.ts` proxies `/v1` to it in both dev and preview.
 *
 * P2-10 repair: no mandate URL is ever constructed by this test. Every navigation is a real
 * click — "Open case" (from the feed's Chorus signal), "My mandate" (the header nav link
 * resolved from `GET /session`, P2-3), and "Return to case" (on the mandate thread page).
 * Every assertion is on durable, server-rendered state (a commitment's own `DUE`/`READY FOR
 * ACTION` text in the DOM), never on a transient operation banner that unmounts once its
 * gating condition changes (P1-1's read-boundary fixes mean several of those banners now
 * disappear the instant the underlying state moves on, which is correct — the fix here is to
 * stop asserting on them instead of relying on incidental timing).
 */

const SENTINEL = "Leela";

async function switchPersona(page: Page, actor: string) {
  await page.getByLabel("Active demo persona").selectOption(actor);
}

async function decideOwnMandate(page: Page) {
  const myMandate = page.getByRole("link", { name: "My mandate" });
  await expect(myMandate).toBeVisible({ timeout: 10_000 });
  await myMandate.click();

  const approveButton = page.getByRole("button", { name: "Approve" });
  const appeared = await approveButton
    .waitFor({ state: "visible", timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (!appeared) return; // This resident owns no facts in this case run.

  await approveButton.click();
  await expect(page.getByText("Mandate status: APPROVED")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("link", { name: "Return to case" }).click();
}

async function runHeroFlow(page: Page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Ambient CHORUS" })).toBeVisible();

  // -- Reset -----------------------------------------------------------------------------
  await page.getByRole("button", { name: "Reset demo" }).click();
  await expect(page.getByText(/Reset complete/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("listitem").first()).toBeVisible();

  // -- Discovery ---------------------------------------------------------------------------
  await page.getByRole("button", { name: "Detect pattern" }).click();
  const caseLink = page.getByRole("link", { name: "Open case" }).first();
  await expect(caseLink).toBeVisible({ timeout: 15_000 });

  // -- Open the case, propose mandates -------------------------------------------------------
  await caseLink.click();
  await expect(page.getByRole("heading", { level: 2 })).toBeVisible();
  await page.getByRole("button", { name: "Propose mandates" }).click();
  await expect(page.getByRole("button", { name: "Run investigation" })).toBeEnabled({
    timeout: 15_000,
  });

  // -- Every resident decides their own mandate, reached only through "My mandate" ------------
  for (const actor of ["resident_a", "resident_b", "resident_c", "resident_d"]) {
    await switchPersona(page, actor);
    await decideOwnMandate(page);
  }

  await switchPersona(page, "presenter_admin");

  // -- Investigation -------------------------------------------------------------------------
  await page.getByRole("button", { name: "Run investigation" }).click();
  await expect(page.getByText("Investigation: done.")).toBeVisible({ timeout: 15_000 });
  // The private panel is *supposed* to show the sentinel (P2-8) — that is the whole point of
  // the boundary the compile step below proves. The absence assertion belongs on the
  // shareable panel only, once a view has actually been compiled.
  const privatePanel = page.locator('[aria-labelledby="private-investigation-heading"]');
  await expect(privatePanel).toContainText(SENTINEL);

  // -- Compile: the deterministic boundary must show a REAL exclusion (P2-8) -------------------
  await page.getByRole("button", { name: "Compile shareable view" }).click();
  await expect(page.getByText(/Included:/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/Excluded: [1-9]/)).toBeVisible();

  const shareablePanel = page.locator('[aria-labelledby="shareable-view-heading"]');
  await expect(shareablePanel).not.toContainText(SENTINEL);
  await expect(shareablePanel).not.toContainText("asthma");

  // -- Propose action ----------------------------------------------------------------------------
  await page.getByRole("button", { name: "Propose action" }).click();
  await expect(page.getByText("Action proposal: done.")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "Action proposal preview" })).toBeVisible();

  // -- Approve as case approver (no navigation needed: same case page, persona switch only) ------
  await switchPersona(page, "case_approver");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("button", { name: "Execute / send" })).toBeVisible({
    timeout: 10_000,
  });

  // -- Execute -----------------------------------------------------------------------------------
  await page.getByRole("button", { name: "Execute / send" }).click();
  await expect(page.getByText("Sent.")).toBeVisible({ timeout: 15_000 });

  // -- External reply -> commitment ---------------------------------------------------------------
  await switchPersona(page, "presenter_admin");
  await page.getByRole("button", { name: "Deliver reply" }).click();
  // The reply control unmounts itself the instant the case leaves ACTIONED (it only renders for
  // that state) — the commitment appearing in the durable timeline is the real proof of success.
  await expect(page.getByText(/PENDING/)).toBeVisible({ timeout: 15_000 });

  // -- Advance demo clock, then assert the commitment's own DURABLE status ------------------------
  await page.getByRole("button", { name: "Advance clock past due date" }).click();
  // Not the ephemeral "Watcher outcome" banner (DemoClockControl unmounts once the commitment
  // leaves PENDING) — the commitment's own status badge in the timeline, which is
  // server-rendered and stays on screen regardless of any control's own lifecycle.
  const commitmentList = page.getByRole("list", { name: "Commitments" });
  await expect(commitmentList.getByText("DUE", { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- Verify as the affected resident (no navigation: VerificationPanel is on this page too) -----
  let verified = false;
  for (const actor of ["resident_a", "resident_b", "resident_c", "resident_d"]) {
    await switchPersona(page, actor);
    const missedButton = page.getByRole("button", { name: "Missed" });
    const appeared = await missedButton
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(() => true)
      .catch(() => false);
    if (appeared) {
      await missedButton.click();
      verified = true;
      break;
    }
  }
  expect(verified, "no resident could verify the due commitment").toBe(true);

  await switchPersona(page, "presenter_admin");
  await expect(page.getByText("Ready for action", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  // ACTIONED != RESOLVED — the demo's own thesis, asserted on the durable state stepper: the
  // current step is READY_FOR_ACTION, and RESOLVED does not carry `aria-current`.
  await expect(page.getByText("Ready for action", { exact: true })).toHaveAttribute(
    "aria-current",
    "step",
  );
  await expect(page.getByText("Resolved", { exact: true })).not.toHaveAttribute("aria-current");
}

test("reset, discovery, mandate, investigation, compile, action, approve, send, reply, commitment, clock, verify", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await runHeroFlow(page);
});
