# Five-minute demo runbook

This is the implementation target, not a claim that commands currently exist. The live demo uses only the `DEMO` namespace and the exact three product surfaces.

## Preflight (30–60 minutes before)

1. Confirm the deployed version reports expected `policy/v1`, compiler/template/prompt versions, Nova 2 Lite inference-profile targets, and AgentCore endpoint versions.
2. Confirm Bedrock/AgentCore quota/access, SES verified sender/recipient/configuration set, EventBridge Scheduler role/DLQ, destination secret, CloudWatch alarms, and demo access token.
3. Run `uv run chorus-demo preflight --namespace DEMO`. It must execute non-sensitive runtime schema smokes, IAM-deny canaries, compiler allow/deny canaries, and a sender mailbox-simulator/controlled-address check. It must not create the elevator case outputs.
4. Verify no `SENDING` or `SEND_UNKNOWN` execution exists. An unknown outcome is a hard stop for another live send until reconciled/reset is safely allowed.
5. Run:

   ```text
   uv run chorus-demo reset --namespace DEMO --confirm "RESET DEMO" --seed elevator/v1
   ```

6. Open the feed surface, enter token, select presenter persona, verify 24 messages and logical time. Keep browser zoom/layout rehearsed; do not open private AWS consoles during the five-minute path.

Runtime warm-up may invoke separate harmless schema fixtures. It may not precompute Monitor/Investigator/Action output for the elevator inputs, compile its view, approve, send, create its commitment, or fire its watcher.

## Live script

### 0:00–0:30 — Ambient feed

- Say: “These are ordinary messages residents already send; most are noise.”
- Start/reveal feed playback through the ingestion endpoint.
- Briefly point at package, parking, plumbing, and elevator fragments; do not select a pre-labeled elevator case.

Expected proof: raw feed items persist with stable IDs and unrelated noise remains unhighlighted.

### 0:30–1:00 — Pattern detected

- Let the live Monitor operation finish and the UI poll it.
- Open “Potential recurring issue detected.”
- Say: “One complaint is easy to ignore. Chorus finds the pattern.”

Expected proof: linked fragments arose from validated agent output, no hard-coded report IDs, and the case is `CANDIDATE/AWAITING_MANDATES` only after deterministic application guards.

### 1:00–1:30 — Private mandates

- Switch to Resident B. Show incident/photo/health/name/unit as separate fact permissions and identity as a separate control.
- Adjust: incident date/impact anonymous, photo external-action, name/unit/health internal, identity false. Approve the adjusted immutable version.
- Use rehearsed persona switching to submit A/C/D safe approvals; the UI must still issue real API decisions.

Expected proof: private ownership, complete terms/destination/purpose, versioned decisions. No model broadens a grant.

### 1:30–2:15 — Skeptical investigation

- Run/open the live Investigator operation.
- Show six incidents, four residents, duplicate-reporter/root independence, evidence statuses, and management contradiction.
- Say: “Corroboration is two independent sources. Aggregate privacy is three contributors. They are different tests.”

Expected proof: deterministic independent count and state guard; contradiction is retained, not smoothed away.

### 2:15–3:00 — Privacy attack and compile boundary

- In the private panel, show the malicious instruction and Resident B's mother name/health/unit.
- Compile live. The side-by-side view shows those items denied/excluded and only authorized safe facts/evidence ref included.
- Point at the audit row and view hash.
- Say: “We do not ask the model to remember what is secret. We never give the external agent the secret in the first place.”

Expected proof: compiler-produced immutable view; secret sentinels absent from safe panel/Action input; injected text has no authority.

### 3:00–3:45 — Action, approval, and SES

- Run the Action proposal and show claims with safe export citations.
- Show deterministic email preview, destination label, proposal/view/preview hashes.
- Switch to approver, approve exact preview, execute once.
- Show `SENT` and SES message ID/controlled inbox receipt. Say “Approval authorizes one immutable attempt; it is not the send itself.”

Expected proof: Action has no private/tool/data access, sender alone calls SES, case becomes `ACTIONED`, not resolved.

### 3:45–4:30 — Manager promise and real schedule

- Deliver the staged management reply through the inbound boundary. **On the deployed demo this is a real email**, sent from the verified correspondent identity to the receiving address and authenticated by SES's own SPF/DKIM/DMARC verdicts — the fixture-selector route below is the `test`/`development` mechanic, because `LocalInboundMailAuthenticator` refuses to construct in `demo` and the AWS composition passes `authenticator=None` ([the deployment contract](phase-11-deployment-contract.md) § 10.2). The scripted part is the wording; the transport, the authentication, and the correlation are real. Its plain-text body states an explicit ISO date — “We will restore elevator B to service by 2030-01-14.” — because [ADR-021](../adr/ADR-021-action-grounding-and-caveats.md) § 6 rejects weekday and relative date constructs outright and [ADR-027](../adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 4 will not let a model turn “Wednesday” into an instant. The presenter selects a **fixture identifier**; the route reads no case, destination, sender, or body from the caller, and the fixture goes through the same attester a deployed delivery would ([ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md)).
- Let the `EXTRACT_COMMITMENT` operation propose span-cited terms; the nine deterministic checks create the commitment, and the scheduler adapter creates a real one-time schedule under the deterministic name and client token.
- Show logical due time, real mapped schedule time, generation, and case `VERIFYING`.

Expected proof: the reply is authenticated by its transport and correlated to one `SENT` execution before it is anything at all; the raw MIME stays private; the obligor comes from the correlation and the deadline from a cited ISO date, neither from the model; and the scheduler — not agent memory — owns the deadline.

### 4:30–5:00 — Background watcher and missed deadline

- Advance demo logical time past the due time. This invokes the deployed watcher with the same due-event contract.
- Show one verification request and replay-safe event ID.
- Switch to Resident A and mark `MISSED` (“elevator still unavailable”).
- End on case `READY_FOR_ACTION` and commitment `MISSED`.

Expected proof: `ACTIONED ≠ RESOLVED`; the system closes the accountability loop without falsely closing the case.

## Failure handling during the demo

- **Agent failure:** show typed operation failure and explain the deterministic state did not advance. One normal retry with the same operation ID may occur; do not load canned output.
- **Compiler denial:** show reason and fix only by an actual mandate/state decision. Never bypass policy.
- **SES explicit failure:** show `FAILED`; do not claim sent. A second attempt requires a fresh proposal/action/approval and probably exceeds demo time.
- **SES timeout/unknown:** stop sending, show `SEND_UNKNOWN`, explain duplicate prevention, use the recorded backup only to continue the presentation narrative.
- **Scheduler failure:** show the commitment as unscheduled and typed error; use recorded backup rather than direct state mutation.
- **Network/UI failure:** the backup video/screenshots may demonstrate a prior genuine run, clearly labeled recorded.

The fallback is presentation media, never a hidden fake-success code path or preseeded domain result.

## Post-demo

Capture safe evaluation/CloudWatch IDs, reconcile any unknown send, delete/disable demo schedule through reset, rotate the demo token if shared, and run the exact reset/cleanup receipt. Do not delete tables/buckets broadly and do not claim the missed case resolved.
