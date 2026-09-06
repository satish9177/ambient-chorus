# ADR-025: One deliberate SES attempt — the send order, the fence's real job, and the honest unknown

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § ActionExecution; [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) § Freshness and send authorization fence; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Action pipeline, § Deterministic rendering, § Destination and SES controls, § Idempotency and ambiguous sends, § Failure/retry classification; [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary, § Propose, approve, execute; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Required events, § Complete failure matrix; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register; [ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 1's presence table

## Context

### The frozen order cannot be executed

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md)'s pipeline diagram runs:

```text
consume approval; APPROVED -> SENDING
acquire fence
deterministic render + hash
SendEmail
```

[ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 1's presence table makes `rendered_message_hash` and `ses_request_token_hash` **required** at `SENDING`. The first step therefore has to write two values that the third and fourth steps have not produced yet. The order as drawn cannot be run against the entity the same freeze created.

### The fence has been asked to do a job it cannot do

The fence is keyed `NS#n#FENCE#k` — one per **case** — and its acquire semantics let the same `execution_id` replay its own live fence. So the fence does not, and by construction cannot, stop a second worker from sending the same approved execution: the second worker presents the same execution ID and is handed the same fence.

That is not a defect in the fence. The fence exists to order a send against a *mandate revocation*, which is a per-case race, and [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) describes exactly that. But Phase 8 must say plainly which mechanism prevents a duplicate send, because "the fence" is the intuitive wrong answer and it is one an implementer would reach for.

### `ses_request_token_hash` has no definition

The field is required at `SENDING` and is described only as "minted by the sender immediately before the SES call" and as something that "aids correlation but is not treated as SES deduplication". No formula exists. Neither does one for the `execution_id_hash` email tag the configuration set is supposed to publish events under.

### The SES payload is not specified, and "approved bytes equal sent bytes" is therefore unproven

The renderer's output is a subject, a plain-text body, and an HTML body. What SES is actually handed — which API shape, which charset, which headers, whether `Raw` or `Simple`, what `Reply-To` — is nowhere frozen. Without it, the invariant the whole phase exists to deliver reduces to a claim about two hashes of the same tuple, which is true and says nothing about the message that left the building.

### Reconciliation has no caller

"A reconciliation task after the 60-second fence expiry marks it `SEND_UNKNOWN`" names no component, no trigger, and no schedule. V1 has no background reconciler and Phase 9 owns the only scheduler in the system.

## Decision

### 1. The duplicate-send boundary is the claim compare-and-swap, and it is the only one

> **At most one deliberate SES call is made per `ActionExecution`, and the thing that guarantees it is the conditional `APPROVED@v → SENDING@v+1` write.**

Everything else is defence in depth or serves a different purpose:

| Mechanism | What it actually prevents |
|---|---|
| **claim CAS** `APPROVED@v → SENDING`, carrying the attempt's `claim_owner_hash` | two processes both proceeding to SES for one execution |
| **never-resend-from-`SENDING`** | the *same* process, redelivered, calling SES a second time |
| **one execution per action**, `attempt_number = 1`, no edge out of `SENT` | a second attempt for one approved message |
| **send fence** (per case) | a mandate revocation and a send both believing they won |
| **send-result idempotency records** | a lost transaction outcome being resolved by re-running rather than by reading |

The claim CAS is sufficient because a process that loses it observes a state — `SENDING`, `SENT`, `FAILED`, or `SEND_UNKNOWN` — from which the frozen replay table forbids an SES call, unconditionally and without inspecting anything else:

```text
DRAFT         -> 409; nothing to send, the approval has not happened
APPROVED      -> claim; exactly one winner proceeds
SENDING       -> 202 in progress; NEVER call SES
SENT          -> 200 with the same message reference
FAILED        -> 409 terminal; a fresh proposal and approval are required
SEND_UNKNOWN  -> 409 quarantine; no retry endpoint exists
```

**The claim must be proved by the row, not by the commit's own answer.** The repair pass found the one interleaving in which the paragraph above was false. Worker A commits the claim; worker B's claim transaction is *lost* rather than rejected --- an ambiguous transport outcome, which is the one thing a caller cannot distinguish from a write that landed. The unit of work resolves that by reading the plan's commit proof, and every Phase-8 send record is keyed on the **execution** (§ 12, domains 4 to 6) precisely so it is replay-safe regardless of who was invoked. So B read A's proof, correctly concluded that *the claim committed*, and incorrectly concluded that **it** had claimed. Two deliberate SES calls for one approved message.

A shared proof answers "was this execution claimed". Only the second question --- "which attempt owns the claim" --- authorizes a sender, and no derivation the send path already had could answer it: `idempotency_key`, `ses_request_token_hash`, and all three send keys are pure functions of durable values, so two workers compute every one of them identically.

`ActionExecution` therefore gains **`claim_owner_hash`**: minted per attempt before the claim, written in the same conditional write that moves the state, and required at `SENDING` onwards. Recovery then reads the durable execution strongly and compares:

```text
SENDING with this attempt's owner   -> this attempt claimed; it may continue
SENDING with another owner          -> somebody else claimed; NEVER call SES
APPROVED, no owner                  -> nothing committed; the same claim may be retried
any terminal state                  -> the replay table forbids an SES call anyway
```

The comparison is made on **every** send rather than only on the ambiguous branch, at the cost of one strongly consistent get: a check that ran only where somebody remembered ambiguity was possible is a check the next change moves out from under.

**Two concurrent send workers** therefore resolve to one SES call and one 202. **A duplicate Lambda delivery** of the same job resolves to the same, because the second delivery reads `SENDING` or a terminal state. Neither outcome depends on the fence.

### 2. What the send fence does, exactly

| Question | Answer |
|---|---|
| who creates it | the **compiler**, inside `AcquireSendAuthorizationFence`; the sender only invokes that operation and holds no DynamoDB access to Core at all |
| granularity | **per case** — `NS#{namespace}#FENCE#{case_id}` / `SEND_FENCE`, one row, holding the acquiring `execution_id` |
| when written | after the claim, immediately before rendering is handed to SES; never speculatively |
| expiry | `min(requested_at + 60s, view.expires_at, approval.expires_at, earliest relied-on mandate expiry)`; fewer than **5 seconds** of remaining authority denies rather than racing |
| who may mutate it | the compiler role only, scoped by `LeadingKeys` to `NS#*#FENCE#*` ([ADR-019](ADR-019-send-fence-partition-isolation.md)) |
| concurrent workers | irrelevant to duplicate prevention; the second worker never reaches acquisition, having lost the claim CAS |
| after `SENT` | **released.** The fence is not the record of the send; the execution row is |
| after `FAILED` | **released** |
| after `SEND_UNKNOWN` | **released** |
| if the process dies | not released; it expires within 60 seconds and mandate mutations resume |

**The fence is released on every terminal outcome, including the ambiguous one**, and that is the load-bearing choice in this table. A fence retained to mark "something happened here" would permanently refuse every future mandate decision and revocation on that case, which inverts the guarantee the fence exists to provide: it is a 60-second ordering window for contributors' authority, not a lien on it. Uncertainty about a send is recorded in `SEND_UNKNOWN`, where it is visible, alarmed, and does not silently disable anybody's ability to withdraw consent.

Release is conditioned on the holder's `execution_id`, so a late or crashed process cannot clear a fence that has since been taken by another.

### 3. The frozen send order

Rendering is a pure function of immutable inputs with no side effect, so it can happen before the claim at no cost — and the presence table means it must. The order is normative:

```text
 1. strong-load, from Shareable: the current action pointer, the proposal,
    the approval, the execution, and the exact bound view
 2. verify locally, calling nothing external:
      pointer names this action and execution, status DRAFT
      proposal_hash recomputes; view_hash recomputes
      approval_hash recomputes; decision == APPROVED; approval names this execution
      now < approval.expires_at   and   now < view.expires_at   (equality is expired)
      execution.state == APPROVED at the expected version; approval_id matches
      view.destination.{destination_id, registry_version, routing_token, display_label}
        equal the deployment's current registry entry, by exact equality
      template_version and from_identity_id equal the deployment's current values
 3. render deterministically from {proposal, view, template_version, from_identity_id}
 4. rendered_message_hash := preview digest of that render
    REQUIRE rendered_message_hash == proposal.preview_hash
      -> mismatch is a definite pre-send failure: FAILED / STALE_AUTHORIZATION, no SES call
 5. ses_request_token_hash := the § 5 derivation
5b. claim_owner_hash := § 7's derivation over a nonce minted for THIS attempt
 6. CLAIM  transaction C: APPROVED@v -> SENDING@v+1, writing rendered_message_hash,
    ses_request_token_hash, claim_owner_hash, started_at
6b. strong-read the execution; REQUIRE SENDING carrying THIS attempt's claim_owner_hash
      -> another owner: no SES call, nothing written, answer from the durable row
 7. AcquireSendAuthorizationFence   (the compiler takes the fence and revalidates the
    whole case side from inside it)
      -> DENY: transaction D, SENDING -> FAILED / STALE_AUTHORIZATION, no SES call
 8. require now < fence.expires_at, sampled again from the injected clock
 9. resolve the recipient from the destination-registry secret; assert exactly one
9b. require now < fence.expires_at AGAIN, after every awaited resolution and after the
    payload is built, with nothing between this sample and the call
10. ONE ses:SendEmail
11. persist the outcome: transaction E, D, or F
12. ReleaseSendAuthorizationFence, in a finally
```

Step 4 is the invariant the phase exists for, and it is a comparison rather than a tautology only because the two digests have different owners and different moments ([ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 2). It fires when the template version, the sending identity, or the destination routing has changed since approval, because all four are inside the digest.

**Steps 6b and 9b are added by the Phase-8 repair pass**, and each closes a window the original order left open. 6b is § 1's claim-ownership proof. 9b exists because step 8's sample sits *before* two awaited registry lookups --- a destination resolution and an identity resolution, each of which reaches a secret store --- and a clock read that a later `await` can invalidate is not a check on the instant that matters. The fence expiry is `min(now + 60s, view.expires_at, approval.expires_at, earliest relied-on mandate expiry)`, so the single comparison at 9b is every frozen temporal boundary at once; checking them separately there would be four ways to spell one number, and the ways would drift.

**The transport timeout is deliberately not changed.** An SES call that begins with five seconds of fence remaining can still be in flight when the fence lapses, because botocore's default read timeout is sixty seconds. Shortening it would not make the safety property stronger --- the property is about the *number of deliberate attempts*, which a timeout does not affect --- and it would make the system strictly worse: a read timeout is classified `SEND_UNKNOWN` (§ 8), so an aggressive one converts slow-but-successful sends into quarantines that no path may ever retry. What is pinned instead is that the read timeout does not *exceed* the fence's maximum life, so an in-flight request cannot outlive the window by more than the window itself, and that the connect timeout is short, because a failed connection is a **definite** non-send. § 5's mirror ordering already records the residual: once an attempt has started, the message cannot be recalled.

Step 2's expiry checks and step 8's clock re-read exist for the reason [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) already gives about the proposal's second clock sample: **expiry is the one freshness fact no storage condition can express**, because the passage of time mutates no row.

### 4. Send-time authorization: what is checked, by whom, and what must not be

The sender has no Core access, so it cannot check the case at all. The case-side checks are the **compiler's**, inside fence acquisition, which is why that operation exists and why it takes a request rather than an execution ID.

**The order inside that operation is: take the fence, *then* revalidate.** The Phase-8 repair pass found the race the other order leaves. Authorization is validated; a contributor's revocation commits; the fence is then acquired, and acquisition checks only the fence. Neither half saw the revocation --- the validation had already run, and the acquisition was never looking --- and the message went out under a mandate that had been withdrawn. Reproduced end to end: mandate `REVOKED`, case `INVESTIGATING`, execution `SENT`, one SES call.

Acquisition is not a decision; it establishes the sixty-second ordering window in which a decision may be made. While the fence is live every authorization-sensitive Core mutation fails its `ConditionCheck`, so nothing the revalidation reads can move underneath it. A denial releases the fence immediately, and so does an integrity failure raised inside the revalidation, so a refused send holds the case for exactly the duration of its own reads rather than for sixty seconds of somebody else's authority.

The **sender's** boundary is unchanged by this and must stay so: both halves of the fence and every check below are reached through `chorus.ports.send_authorization.SendAuthorizationPort`, which a deployed sender satisfies with an `lambda:InvokeFunction` adapter against the compiler and no Core handle of any kind (§ 16).

`SendAuthorizationRequest`, frozen:

```text
{ namespace, community_id, case_id, action_id, execution_id, approval_id,
  proposal_hash, view_id, view_hash, authorization_version,
  policy_version, compiler_version, policy_build_hash,
  destination_id, destination_registry_version, routing_token, purpose,
  authorization_snapshot_hash, requested_at }
```

| Checked | At approval (application) | At send (compiler, in the fence) |
|---|:--:|:--:|
| case lifecycle **state** | `ACTION_PROPOSED` | `ACTION_PROPOSED` |
| `case.authorization_version` == proposal's and view's | yes | yes |
| `CommunityCase.version` == proposal's `case_version` | **never** | **never** |
| view integrity: `view_hash` recomputes, pointer names it | yes | yes |
| view expiry, `now < expires_at` | yes | yes |
| proposal integrity: `proposal_hash` recomputes | yes | yes |
| approval integrity: `approval_hash` recomputes, decision, execution binding | n/a — being created | yes |
| approval expiry | n/a | yes |
| execution integrity: state and expected version | yes | yes, by the claim CAS |
| current mandate pointers, versions, revocations, expiry | via `authorization_snapshot_hash` recomputation | **recomputed from live Core state** and compared |
| destination registry version and routing token | yes | yes |
| policy build, compiler version, policy version | yes | yes |
| `template_version`, `from_identity_id` | yes | yes, via `preview_hash` equality at step 4 |
| current action pointer identity and status | yes | yes |

The **`CommunityCase.version` row is the one that matters most**, and it is a prohibition rather than a check. Lifecycle progression moved that number on purpose — the `READY_FOR_ACTION → ACTION_PROPOSED` edge that created this proposal moved it — and requiring equality would fail every first send in the system. [ADR-020](ADR-020-case-authorization-version.md) § 6 removed that deadlock and this ADR does not reintroduce it at a second site.

`authorization_snapshot_hash` recomputation is what makes the mandate row a real check rather than a version comparison: the compiler rebuilds the snapshot from live Core state and requires exact equality with the view's, so a revoked, adjusted, expired, or newly-superseded mandate denies even if every counter happened to line up.

### 5. Revocation after approval, and the other five late changes

The concrete race, resolved:

```text
T1  human approves                     execution APPROVED,  case.authorization_version = A
T2  contributor revokes a mandate      mandate decision transaction requires no live fence;
                                       none is live, so it commits: A -> A+1
T3  sender claims                      APPROVED -> SENDING           (succeeds; Shareable only)
T4  sender acquires the fence          compiler reloads Core: A+1 != A
                                       -> DENY STALE_AUTHORIZATION
T5  sender                             SENDING -> FAILED / STALE_AUTHORIZATION
                                       NO SES CALL. Fence never existed; nothing to release.
```

**The old approval does not authorize the send**, and it does not because the approval is not consulted for authority at T4 — it is consulted for integrity. Authority is re-derived from live state every time.

The audit record is `action.send.failed` with reason codes `STALE_AUTHORIZATION` and `AUTHORIZATION_VERSION_MOVED`, carrying the proposal's epoch and the case's current one as integers. The approval row is untouched and remains the durable record that a human did approve something, which is exactly the fact an operator needs afterwards.

The mirror ordering is the other half of T09 and is unchanged: if the fence commits first, the revocation gets a retryable 409 for at most 60 seconds and then commits, and the sent message cannot be recalled.

The other five late changes resolve through the same machinery, each at its named step:

| Late change | Detected at | Outcome |
|---|---|---|
| **view expiry** | step 2 (`now < view.expires_at`) and again inside the fence | `FAILED / VIEW_EXPIRED`, no claim if caught at step 2, no SES call either way |
| **destination registry version change** | step 2 exact equality, and step 4 via `preview_hash`, and the fence | `FAILED / DESTINATION_REGISTRY_CHANGED` |
| **routing token change** | identically, three times over | `FAILED / ROUTING_TOKEN_CHANGED` |
| **policy or compiler build change** | step 2 exact equality and the fence | `FAILED / STALE_AUTHORIZATION` |
| **sender identity mapping change** (`from_identity_id`) | step 2, and structurally at step 4 because it is inside `preview_hash` | `FAILED / SENDER_IDENTITY_CHANGED` |
| **current proposal superseded** | step 2 pointer identity, and the claim CAS, because superseding requires invalidating this execution to `FAILED` first | `409`; the execution is already terminal |

Each is a **definite** failure with no automatic retry and no SES call, and each is remedied the same way: recompile, re-propose, reapprove. That uniformity is deliberate — a late authorization change is never a reason to try again with the same artifacts.

### 6. The SES payload, frozen field by field

```text
API                          SESv2 SendEmail
FromEmailAddress             registry.from_address        resolved from from_identity_id
Destination.ToAddresses      [ registry.destination_address ]   exactly one; asserted
Destination.CcAddresses      omitted
Destination.BccAddresses     omitted
ReplyToAddresses             [ registry.reply_to_address ]      exactly one
FeedbackForwardingEmailAddress   omitted
ConfigurationSetName         settings.ses_configuration_set     "chorus-{environment}"
EmailTags                    [ { Name: "chorus_execution", Value: <§ 7 tag> } ]
Content.Simple.Subject       { Data: subject,   Charset: "UTF-8" }
Content.Simple.Body.Text     { Data: text_body, Charset: "UTF-8" }
Content.Simple.Body.Html     { Data: html_body, Charset: "UTF-8" }
Content.Raw                  NEVER USED
Content.Template             NEVER USED
ListManagementOptions        omitted
```

**`Content.Simple`, never `Content.Raw`.** SES composes the `multipart/alternative` structure and every header itself, so the sender never builds a header line and header injection is structurally impossible rather than merely defended against. [ADR-021](ADR-021-action-grounding-and-caveats.md) § 5 already rejects control characters in every model-authored field; `Simple` means that ban is a second layer rather than the only one.

**`from_identity_id` resolves to a pair, not an address.** The sender's destination-registry secret holds one entry keyed by the identity ID:

```text
{ identity_id, from_address, reply_to_address, identity_arn }
```

so binding the opaque ID in `preview_hash` binds the whole letterhead — sender and reply path together — while no artifact a model, a human preview, an audit row, or a log line can see ever contains either address. A `Reply-To` that could vary independently of an approved `From` would be an approval of the words and not of the correspondence.

**What "approved bytes equal sent bytes" therefore means, exactly.** The subject, the plain-text body, the HTML body, the sending identity, and the destination routing triple are bound by `preview_hash` and re-verified at step 4. The recipient address, the reply-to address, and the configuration set are *derived deterministically* from bound values through a registry the human cannot see. Nothing between step 4 and step 10 is editable, and no field of the payload is sourced from anywhere but the render output and that registry. The claim is bounded and it is provable; a broader claim would not be.

**No third digest is introduced over the wire payload.** `rendered_message_hash` is the preview digest recomputed at send time, and it is that precisely so it is comparable to something a human approved. A hash of the SES request would be comparable to nothing, and it would be a second version of the truth about bytes the first one already fixes.

### 7. The three derived values

```text
claim_owner_hash = hash_value({
    "domain": "send-claim-owner/v1",
    "namespace": namespace,
    "action_id": action_id,
    "execution_id": execution_id,
    "claim_nonce": <a UUID minted for THIS attempt, before the claim>,
    "attempt_number": 1,
})

ses_request_token_hash = hash_value({
    "domain": "ses-request-token/v1",
    "namespace": namespace,
    "action_id": action_id,
    "execution_id": execution_id,
    "idempotency_key": execution.idempotency_key,
    "attempt_number": 1,
})

execution_tag = the 64 lowercase hex characters of hash_value({
    "domain": "execution-tag/v1",
    "namespace": namespace,
    "execution_id": execution_id,
})
```

`claim_owner_hash` is the odd one out and deliberately so: it is the **only** derivation on this path that is not recomputable from durable state, because a value two racing workers could both compute cannot distinguish them. The nonce is minted per attempt and hashed rather than stored raw, for the reason every other identifier on a Shareable row is: what is persisted proves a match and is never a value that could be presented as a credential.

The other two are deterministic functions of durable values, so a recovery path can recompute either without having stored it. The tag is **bare hex with no `sha256:` prefix**, because SES email-tag values admit only `[A-Za-z0-9_-]` and a colon would be rejected at the API — a detail worth freezing rather than discovering at the first live send.

`ses_request_token_hash` is a correlation value and **is not treated as an SES deduplication token**, because SES `SendEmail` offers no such guarantee. Writing it into the row before the call is what lets an operator tie a CloudTrail entry to an execution when nothing else can.

### 8. The SES outcome classification, with a fail-safe default

> **`FAILED` requires proof. Everything else is `SEND_UNKNOWN`.**

| Observation | State | `failure_code` |
|---|---|---|
| SES returns a message ID | `SENT` | — |
| SES returns an error response (any HTTP status, any SES error code) — including validation errors, an unverified identity, a rejected recipient, throttling, and 5xx | `FAILED` | `SES_REJECTED` for request-shape and identity errors; `SES_DEFINITE_FAILURE` for throttling and 5xx |
| the connection was never established — connect timeout, DNS failure, endpoint unreachable | `FAILED` | `SES_UNREACHABLE` |
| a request was transmitted and no response was received — read timeout, connection reset mid-flight | `SEND_UNKNOWN` | — |
| **any other exception** | `SEND_UNKNOWN` | — |

A **received error response** is proof that SES processed the request and declined it; a message it declined was not queued for delivery. A **failed connection** is proof at the transport layer that no request was transmitted. Everything between those two proofs, and everything not enumerated, is unknown — and the last row is the one that matters, because an exception class nobody anticipated must land on the safe side by default rather than by someone remembering to add it.

The exact exception classes on the definite side are enumerated in code as a closed frozen set, and a test asserts that an unlisted exception yields `SEND_UNKNOWN`.

### 9. The unknown outcome, and what is not claimed

**CHORUS does not deliver email exactly once, and this document does not say that it does.** SES `SendEmail` exposes no client token, no deduplication window, and no idempotent replay, so an ambiguous call cannot be safely repeated. The provable property is narrower and is stated as the safety invariant:

> **At most one deliberate SES attempt is made per approved `ActionExecution`, and an attempt whose outcome is unknown is never repeated by any automatic or manual path.**

That is a *most-once* guarantee about attempts, not an *exactly-once* guarantee about deliveries. A message may have been delivered and recorded as `SEND_UNKNOWN`; that is the residual, it is visible, and the alternative — resending to be sure — would turn an unknown into a certainty of duplication.

Consequences, enumerated for every case the phase must handle:

| Situation | Durable state | SES calls | Recovery |
|---|---|---:|---|
| crash before the claim commits | `APPROVED` | 0 | safe: a redelivery claims and sends |
| crash after the claim, before the SES call | `SENDING` → `SEND_UNKNOWN` | 0 | quarantine; the row cannot distinguish this from the next line, and pretending otherwise would require a marker written non-atomically with the call |
| definite SES failure | `FAILED` + code | 1 | fresh proposal and approval |
| SES success, response received, DB write fails | `SENDING` → `SEND_UNKNOWN`, later reconciled `SENT` | 1 | the SES event carrying the tag and message ID is the positive proof |
| crash immediately after SES accepts | identical to the row above | 1 | identical |
| SES timeout, outcome unknown | `SEND_UNKNOWN` | 1 (unknown) | reconcile only |
| worker retried after any of the above | unchanged | 0 | the replay table refuses from `SENDING` and from every terminal state |
| duplicate worker delivery | unchanged | 0 | same |
| two concurrent send workers | one claims | 1 | the loser gets 202 or 409 |

`SEND_UNKNOWN` is quarantined from every retry command. **There is no retry endpoint**, and its absence is a design element rather than a gap.

### 10. Reconciliation has exactly one command and two callers

`ReconcileSendOutcome` is a single application command. It never calls SES.

| From | To | Requires |
|---|---|---|
| `SENDING` | `SEND_UNKNOWN` | `now >= started_at + 60s` (the fence's maximum life) **and** no live fence for the case |
| `SEND_UNKNOWN` | `SENT` | positive evidence: an SES configuration-set event whose configuration set is the deployment's, whose `chorus_execution` tag equals the recomputed § 7 tag for this exact execution, and which carries a message ID |
| `SEND_UNKNOWN` | `FAILED` | positive evidence that SES never accepted: an SES event, or an operator attestation recorded with its own reason code |

Its two callers are the **application worker**, which invokes it when a replay finds an execution in `SENDING` past the window, and the **trusted configuration-set event path**, which is the only thing that can supply the positive evidence the second and third rows require. Nothing invokes it on a timer, and `SENDING → SEND_UNKNOWN` never happens as a side effect of a read.

**The second caller is an event boundary and not an HTTP route, and the Phase-8 repair pass is where that was settled.** An earlier draft of this section said "operator route", which § 15's frozen API surface never contained and [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary never listed. The two documents disagreed, and the missing route was the better-specified one: a `SEND_UNKNOWN → SENT` edge needs an SES message identifier, and a message identifier is a value **only SES can produce**. An endpoint accepting `{configuration_set, execution_tag, message_id}` from its caller would be an endpoint through which anybody holding the demo token resolves a quarantine by typing three strings --- T35 with the forger handed the pen.

**Trusted evidence is defined as follows.** It is a configuration-set event notification delivered by SES itself through the deployment's own event destination. Every field of the resulting evidence is read out of that envelope and none of it is caller-supplied: the configuration set comes from the event's `ses:configuration-set` tag, the execution tag from its `chorus_execution` tag, the identifier from `mail.messageId`, and acceptance from an enumerated `eventType` --- `Send` and `Delivery` prove SES took the message, `Rendering Failure` proves it never queued anything, and every other type is refused rather than interpreted. Cross-execution replay is refused by the § 7 tag derivation, which is recomputable for the execution being reconciled and matches exactly one.

**Structure is not provenance, and the second repair pass is where that was settled.** Everything in the paragraph above describes what an envelope *says* and how it *correlates*; none of it establishes where the envelope came from. The gap was reproduced: a caller-built `Delivery` envelope naming the right configuration set, carrying the recomputed § 7 tag and an invented `mail.messageId`, decoded cleanly, correlated cleanly, and moved a quarantined row to `SENT` under the invented identifier. Every check had done its job. None of them was ever about origin.

The boundary is therefore two halves that are never the same object, over one process-local attestation key:

| Half | Held by | Can |
|---|---|---|
| attester | the SES event adapter, and nothing else | require an authenticated transport, require this deployment's own event destination, decode, and **mint** attested evidence |
| verifier | `ReconcileSendOutcome` | **check** an attestation, and nothing more |

The order inside the attester is fixed: authenticate the transport, *then* decode. Decoding first would mean a forged envelope had already been interpreted by the time anybody asked where it came from. `SEND_UNKNOWN → SENT` accepts only the attested wrapper, so a caller-built mapping, a caller-built evidence object, a wrapper edited after minting, and evidence from another deployment's boundary are all refused under `UNATTESTED_EVIDENCE` before any state is read. Authentication and correlation remain two separate questions and both are still asked --- a genuine event about another execution is still refused by the § 7 tag.

**Phase 8 owns the boundary; Phase 11 owns the subscription that feeds it** --- the event destination on the `chorus-{environment}` configuration set and the authenticated transport that carries the notification --- which is the same static-now, live-in-Phase-11 split as the sender function, its role, and the configuration set itself (§ 16). Concretely, Phase 11 owes exactly one object: a `SesEventTransportAuthenticator` over the transport it builds. **Phase 8 implements none**, and that absence is deliberate --- a permissive stand-in would be indistinguishable at the call site from a real one. With no authenticator there is no attester, with no attester no evidence, and with no evidence a `SEND_UNKNOWN` row stays `SEND_UNKNOWN`, which § 9 already names as the correct outcome. The evidence-free `SENDING → SEND_UNKNOWN` path is unaffected and still works in a deployment with no event transport at all.

An **operator attestation** remains on the command and remains sufficient for `SEND_UNKNOWN → FAILED` only, because a person can responsibly say something did not happen and cannot produce a message identifier. It has no V1 transport, and giving it one is Phase-11 work alongside the operator surface that would carry it.

`SEND_UNKNOWN → SENT` and `→ FAILED` already require `reconciliation_proof` in `chorus.domain.state.transition_action_execution`; this ADR is what the flag means. A message ID that disagrees with one already recorded is an `IntegrityError` and never an overwrite — monotonic presence refuses the rewrite, and a **forged or tampered SES message ID is therefore, at worst, a rejected reconciliation**, never a silent replacement of a recorded outcome. Uncertainty remains unknown indefinitely rather than being resolved by anything short of proof.

### 11. The six transaction shapes

Participant counts are fixed per shape and asserted arithmetically against the staged plan, in the manner Phase 5, Phase 6, and Phase 7 already are. **None of them is ten.** The proposal apply needed ten because it commits an artifact, an execution, two pointers, an invocation record, a case transition, and two guards at once; a send outcome commits one row and its proof.

**C — pre-send claim** (sender; Share/Audit; **3**)

| # | Participant | Table | Kind |
|---:|---|---|---|
| 1 | execution `APPROVED@v → SENDING@v+1`, writing `rendered_message_hash`, `ses_request_token_hash`, `started_at` | Share | conditional Put on the exact version and state |
| 2 | `action.send.started` `AuditEvent` | Audit | append-only Put |
| 3 | completed send-claim idempotency record and commit proof | Share (`EXECUTION` partition) | create-only Put |

No fence check: a fence can only be held by an execution that has already passed `APPROVED`, and participant 1's own condition is that this one has not. No approval `ConditionCheck`: the approval is immutable, so the strong read at step 2 is not a value a condition could improve on.

**D — definite send failure**, **E — send success**, **F — unknown outcome** (sender; Share/Audit; **3** each)

| # | Participant | D | E | F |
|---:|---|---|---|---|
| 1 | execution transition from `SENDING@v+1`, conditional on the exact version | `→ FAILED`, `failure_code`, `failure_detail_safe?`, `finished_at` | `→ SENT`, `ses_message_id`, `finished_at` | `→ SEND_UNKNOWN`, `finished_at` |
| 2 | audit event | `action.send.failed` | `action.sent` | `action.send.unknown` |
| 3 | completed send-result idempotency record and commit proof | same | same | same |

One shape, three outcomes. D is also the shape used for the pre-SES failures at steps 4 and 7, with `failure_code` `STALE_AUTHORIZATION` and no SES call having been made.

**A — approval** (application; Share/Core/Audit; **5**) and **B — rejection, withdrawal, or clearing** (application; Share/Core/Audit; **6** / **5**), specified by [ADR-023](ADR-023-approval-binding-and-immutability.md) and restated here as counts so the whole phase's arithmetic sits in one table:

| Shape | Participants |
|---|---|
| **A** approve | immutable `Approval`; execution `DRAFT@1 → APPROVED@2`; `action.approved` audit; completed idempotency record and commit proof; Core case `ConditionCheck` on exact `version`, `authorization_version`, `state == ACTION_PROPOSED` |
| **B** reject | the five above with `decision=REJECTED`, the execution moving `DRAFT@1 → FAILED@2`, and `action.rejected`; **plus** the current action pointer `DRAFT → INVALIDATED` conditional on `{row_version, proposal_hash}`; and the Core case participant becomes a **Put** (`ACTION_PROPOSED → READY_FOR_ACTION`) when readiness remains and stays a **CheckItem** when it does not |
| **B′** withdraw / clear | B without the `Approval` row: execution transition **or** a `ConditionCheck` that it is already terminal `FAILED`; pointer `→ INVALIDATED`; `action.invalidated` audit; idempotency record; case Put-or-Check |

**G — the case projection to `ACTIONED`** (application worker; Core/Share/Audit; **4**)

| # | Participant | Table | Kind |
|---:|---|---|---|
| 1 | case `ACTION_PROPOSED@{v,a} → ACTIONED@{v+1,a}` | Core | conditional Put on exact `version`, `authorization_version`, `state` |
| 2 | the execution is `SENT` at its exact version | Share | **ConditionCheck** |
| 3 | `action.actioned` `AuditEvent` | Audit | append-only Put |
| 4 | completed projection idempotency record and commit proof | Share (`EXECUTION` partition) | create-only Put |

`authorization_version` is carried forward unchanged: recording a send outcome changes no fact, status, mandate, or count ([ADR-020](ADR-020-case-authorization-version.md) § 2 row 7). `FAILED` and `SEND_UNKNOWN` take no case edge at all — the case stays `ACTION_PROPOSED` and the surface shows the safe execution banner.

Every shape stays far inside `TRANSACTION_MAX_OPERATIONS`, and every one is independent of how many claims, caveats, or facts the case holds.

### 12. Six idempotency domains, and what each one binds

Separate namespaces, because they are commit proofs for different things and a single reused row would make one outcome answer for another.

| # | Domain | Command family | Partition | Key hash binds | Written by |
|---:|---|---|---|---|---|
| 1 | HTTP approval request | `APPROVE_ACTION` | `ACTION` | `key_hash("approve-action-start\x1f" + client key)`; request hash over `{case_id, action_id, execution_id, decision, expected_execution_version, view_hash, proposal_hash, preview_hash}` | the API route |
| 2 | approval transaction | `APPROVE_ACTION` | `ACTION` | `key_hash("approve-action\x1f" + client key)` | shape A / B participant |
| 3 | send-operation start | `SEND_ACTION` | `NAMESPACE` | `key_hash("send-action-start\x1f" + client key)`; request hash over `{case_id, action_id, execution_id, approval_id, expected_execution_version}` | the API route, completed by `complete_start` |
| 4 | send attempt (the claim) | `SEND_ACTION` | `EXECUTION` | `key_hash("send-claim\x1f" + execution.idempotency_key)` | shape C participant 3 |
| 5 | send-result persistence | `SEND_ACTION` | `EXECUTION` | `key_hash("send-result\x1f" + execution.idempotency_key)` | shape D/E/F participant 3 |
| 6 | recovery and projection | `SEND_ACTION` | `EXECUTION` | `key_hash("send-projection\x1f" + execution.idempotency_key)` | shape G participant 4 |

Domains 4, 5, and 6 key on the execution's own `idempotency_key` — `sha256(namespace | action_id | execution_id | proposal_hash | view_hash | approval_id)`, the frozen formula, unchanged — rather than on a client key. That is what makes them replay-safe regardless of how the worker was invoked or how many times: they identify the attempt, not the request that asked for it.

Retention for the `SEND_ACTION` family is seven days (`SEND_IDEMPOTENCY_TTL_SECONDS`), and it is cleanup only. `send_attempt_is_authoritative` already says why: once an execution is `SENT` or `SEND_UNKNOWN`, that state forbids another attempt for the life of the row, long after any record's TTL.

The two required properties fall out of the table:

- **A duplicate approval creates no new execution and changes no approved bytes.** Domain 1 replays the recorded answer; domain 2's record is the commit proof for a transaction whose own conditions require the execution to still be `DRAFT@1`, which it is not.
- **A duplicate worker delivery makes no second SES call.** Domain 4's record proves the claim committed, and the claim CAS refuses a second one; the replay table refuses to call SES from `SENDING` before any of this is consulted.

### 13. Recovery, by which outcome is unknown

Every branch obeys three rules without exception: **verify immutable artifacts and their hashes first; never invoke the Action model; never repeat an SES call whose previous outcome is unknown.** No recovery path can produce a different proposal, because no recovery path produces a proposal at all.

| Ambiguity | How it is resolved |
|---|---|
| **approval transaction** may or may not have committed | strong-read the execution. `DRAFT@1` → it did not commit; retrying under the same key is safe. Not `DRAFT` → it committed; read domain 2's proof and replay the recorded answer. Proof storage itself unavailable → the caller is told to poll; nothing is retried on a guess |
| **claim transaction** may or may not have committed | strong-read the execution. `APPROVED@v` → the claim did not commit; the sender may claim. `SENDING` → it committed, and the replay table forbids an SES call from `SENDING`; the execution proceeds to reconciliation. This is the deliberately conservative branch, and § 9 records why the alternative needs a marker that cannot be written atomically with an external call |
| **SES succeeded, persistence lost** | the execution stands at `SENDING`; reconciliation to `SEND_UNKNOWN` after the window, then to `SENT` on the configuration-set event carrying the recomputed tag and a message ID |
| **SES timeout or unknown transport** | shape F commits `SEND_UNKNOWN` directly. If *that* transaction's outcome is also unknown, the next reader finds `SENDING` and the previous row applies |
| **post-send transaction** may or may not have committed | strong-read the execution and domain 5's proof. A terminal state is authoritative; `SENDING` with no proof means the result write did not commit, and the same outcome is re-persisted — the result is a pure function of what already happened, so re-persisting it repeats nothing external |
| **projection transaction** may or may not have committed | strong-read the case and domain 6's proof. `ACTIONED` → done. `ACTION_PROPOSED` with no proof → retry shape G, whose own conditions make it safe |

The stale-`RUNNING` timeout for a `SEND_ACTION` operation is gated behind a proof read that succeeded and found nothing, exactly as [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) already requires for the proposal apply: elapsed time is not evidence about a transaction.

### 14. Audit events

Existing naming: dotted, lowercase, entity-first, identifiers and closed codes only.

| `AuditEvent.event_type` | Written by | Entity refs | Reason codes |
|---|---|---|---|
| `action.approved` | application | approval, execution, action | — |
| `action.rejected` | application | approval, execution, action | `PROPOSAL_REJECTED` |
| `action.invalidated` | application | execution, action | `APPROVAL_WITHDRAWN` \| `TERMINAL_EXECUTION_CLEARED` |
| `action.send.started` | sender | execution, approval | — |
| `action.sent` | sender | execution | — |
| `action.send.failed` | sender | execution | one `SES_*` or `STALE_AUTHORIZATION` code, plus a specific cause code |
| `action.send.unknown` | sender | execution | `SES_TIMEOUT` \| `SES_TRANSPORT_AMBIGUOUS` \| `SENDER_PROCESS_LOST` |
| `action.send.reconciled` | application | execution | `RECONCILED_SENT` \| `RECONCILED_FAILED` \| `RECONCILED_UNKNOWN` |
| `action.actioned` | application worker | case, execution | — |

They carry `proposal_hash`, `view_hash`, `preview_hash`, `rendered_message_hash`, `ses_request_token_hash`, the execution and approval identifiers, versions, and `ses_message_id` where known.

**No audit event carries the subject, the plain body, the HTML body, a claim, a caveat, a recipient address, or a reply-to address.** The hashes and identifiers are sufficient to prove which exact message was authorized and sent, and the bodies are regenerable from immutable inputs by anyone entitled to see them; duplicating the external message text into a table with a ninety-day TTL would put the one artifact that leaves the system into a second store for no evidentiary gain. This is the same rule [ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 3 applies to persistence.

The log event names in [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) — `approval.recorded/conflict`, `send.fence.acquired/denied/released`, `execution.sending/sent/failed/unknown/reconciled` — are unchanged and are a different vocabulary for a different purpose. Logs describe what a process did; audit events describe what was decided.

### 15. The API surface, minimal

| Method / path | Role | Result | Guard |
|---|---|---|---|
| `POST /v1/cases/{case_id}/actions/{action_id}/approvals` | case approver | 200 approval + execution | pointer `DRAFT`; execution `DRAFT` at `expected_execution_version`; hashes match |
| `POST /v1/cases/{case_id}/actions/{action_id}/invalidation` | case approver | 200 pointer + execution + case | execution `DRAFT`, `APPROVED`, or terminal `FAILED` |
| `POST /v1/cases/{case_id}/actions/{action_id}/executions` | case approver | 202 send operation | execution `APPROVED` at `expected_execution_version`; `approval_id` matches |
| `GET /v1/cases/{case_id}` | presenter / approver | 200, `current_action.execution` | existing Phase-7 surface, now carrying the terminal state |
| `GET /v1/operations/{operation_id}` | initiating role | 200 status | existing |

Send status is read through the existing case surface and the existing operation poll. **No new read route is added**, because an execution is already part of `current_action` and a second address for one row is a second thing to keep consistent.

Bodies are closed (`extra='forbid'`). The execute body is `{execution_id, expected_execution_version, approval_id}` and accepts no recipient, subject, body, claim, attachment, template, or retry flag — unchanged from [08-api-design.md](../architecture/08-api-design.md) and restated because it is the field list an implementer is most likely to widen.

### 16. Deployment ownership

Phase 8 **owns and synthesizes**: the approval, invalidation, send, reconciliation, and projection commands; `functions/sender` and its composition root; the SES and destination-registry adapters and their local fakes; the configuration-set event reconciliation path --- meaning the decode-and-verify boundary of § 10, not the subscription that feeds it; the compiler-invocation send-authorization adapter and the port both implementations satisfy; the API routes; the sender role, its log group, the SES configuration set, and the CDK template and IAM assertions.

Phase 8 **does not deploy to AWS**. The deployed sender function, the live SES send, the verified-identity and sandbox prerequisites, the configuration-set **event destination** and the transport that carries its notifications, any operator surface for an attestation, and the post-deploy sender IAM and SES canaries belong to **Phase 11**, which is the same static-now, live-in-Phase-11 split already used for the three agent runtimes and for the compiler, and for the same reason: an identity and a policy can be asserted from a synthesized template long before the resource exists, while a canary proving an `AccessDenied` cannot exist without a deployment.

In `test` and `development` the sender writes to a filesystem outbox and makes no network call, as [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § Environment behavior already requires. The one-attempt rule, the claim CAS, the classification table, and the fence apply identically there, so the ambiguous paths are exercised without SES.

## Alternatives considered

- **Keep the frozen order and let the claim write placeholder hashes.** Rejected for the reason [ADR-022](ADR-022-action-draft-preview-and-transaction.md) rejected sentinel digests: a placeholder in a field whose whole purpose is to bind exact bytes is a lie the storage layer then cannot distinguish from a real binding.
- **Make the fence per execution, so it can prevent duplicate sends.** Rejected: the fence's job is ordering a send against a *case-scoped* authorization change, and a per-execution fence would stop blocking revocation, which is the thing it exists to block. The claim CAS already prevents duplicates and does so without an external call.
- **Retain the fence after `SEND_UNKNOWN` as a quarantine marker.** Rejected outright: it would permanently refuse every future mandate decision and revocation on that case. A contributor's ability to withdraw consent must not be collateral damage of an ambiguous send.
- **Write a pre-SES marker so a crash between the claim and the call can be resolved as a definite non-send.** Rejected: the marker is written non-atomically with the call, so it moves the ambiguity one step earlier rather than removing it, and it would create a durable state that *looks* like proof.
- **Automatically retry a `SEND_UNKNOWN` once, on the theory that a duplicate email is a small harm.** Rejected, and named so it is refused once rather than proposed repeatedly. It is not the system's judgement to make about somebody else's correspondence, R07 already records the alternative, and the metric `SEND_UNKNOWN` automatic retries has a frozen target of zero.
- **Treat SES 5xx as ambiguous rather than definite.** Considered seriously and rejected: a received response means SES processed and declined the request, and widening `SEND_UNKNOWN` to cover it would quarantine cases for ordinary throttling. The boundary is drawn at *whether a response was received*, which is observable, rather than at what the response said.
- **Treat a connect timeout as ambiguous.** Rejected for the mirror reason: a connection that never completed transmitted no request, and that is provable at the transport layer. Both boundaries are drawn at proof, in opposite directions, from one rule.
- **Hash the SES request payload as a third digest.** Rejected: it would be comparable to nothing a human approved, and it would be a second version of the truth about bytes `preview_hash` already fixes.
- **Use `Content.Raw` so the sender controls MIME structure and headers.** Rejected: it makes header construction the sender's problem and header injection a live surface, in exchange for control over a structure SES builds correctly.
- **Add a `POST …/executions/{id}/retry` route for definite failures.** Rejected: `FAILED` is terminal for an action by design, and a retry route is the first place somebody would later add a `force` flag for `SEND_UNKNOWN`.
- **Reconcile on a timer.** Rejected for V1: the only scheduler in the system is Phase 9's commitment watcher, and adding a second one to move a row that a worker replay and an operator route already move would be a resource introduced to avoid naming a caller.

## Why chosen

It replaces three claims with mechanisms and one claim with a narrower true one.

"The fence prevents duplicate sends" becomes "the claim compare-and-swap prevents duplicate sends, and the fence orders sends against revocation" — two mechanisms, two jobs, neither borrowing the other's credit. "The sender sends what the human approved" becomes an order of operations in which the comparison happens before the claim and the payload has no field the comparison does not reach. "Reconciliation happens" becomes one command with two named callers and a definition of proof.

And exactly-once delivery becomes at-most-one deliberate attempt, which is what SES can actually support. The narrower statement is the more useful one, because it is the one an operator can rely on at three in the morning.

## Consequences

- `chorus.application.commands` gains `approve_action`, `invalidate_action`, `send_action`, `reconcile_send_outcome`, and `project_action_outcome`; `chorus.application.services.send_fence` gains the `SendAuthorizationRequest` revalidation of § 4, which Phase 6 deliberately left absent.
- `functions/sender` is introduced with a composition root and the same import scan the compiler artifact has: no Strands, no Bedrock client, no agent contract, no scheduler, and no private domain type.
- `ActionExecution.failure_detail_safe` gains a row in [ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 1's presence table — `OPTIONAL` at `FAILED` and **`ABSENT` everywhere else, `SEND_UNKNOWN` included**. The first freeze made it optional at `SEND_UNKNOWN` too, which cannot hold: presence is monotonic and `SEND_UNKNOWN → SENT` is a legal edge whose target has the field absent, so a quarantined row that exercised the option could never be reconciled. An option only one value is reachable from is not an option, and the production code was already declining to write one. The unknown reason is carried by the `action.send.unknown` audit event.
- `ActionExecution` gains `claim_owner_hash`, required from `SENDING` onwards and optional at `FAILED`; `action-execution/v2` becomes `/v3` and the codec accepts no earlier version.
- `chorus.ports.send_authorization` is added, holding `SendAuthorizationRequest`, the two outcome shapes, and `SendAuthorizationPort`. `SendAction.authorization` is typed on the port, the in-process `SendAuthorization` is the local implementation, and `chorus.infrastructure.compiler.send_authorization.CompilerSendAuthorization` is the deployed one. The deployed composition constructs **no** `CoreRepository`, which a static test asserts by walking the object graph.
- `OperationDispatchPort` gains `dispatch_send_action`; `SendActionOperationJob` carries no agent handover, because `SEND_ACTION` invokes no agent and an operation of that kind holding one is refused at construction ([ADR-016](ADR-016-agent-operation-handover-identity.md)).
- [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md)'s pipeline diagram is corrected to the § 3 order; its § Idempotency and ambiguous sends gains the § 9 safety statement in place of any exactly-once reading.
- [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries' "Begin send" bullet is replaced by shapes C through G with their fixed counts.
- New failure-matrix rows for the classification table's five outcomes, for a `preview_hash`/`rendered_message_hash` mismatch at step 4, and for a reconciliation whose message ID disagrees with one already recorded.
- Threat register gains **T34** (an ambiguous send resolved by resending, producing a duplicate external message) and **T35** (a forged configuration-set event reconciling a `SEND_UNKNOWN` to `SENT`), both mitigated above.
- The Phase-8 repair regression suite adds, at minimum: an ambiguous claim after a foreign commit making exactly one SES call; an ambiguous claim that did not commit resuming safely; a foreign claim owner never sending; a real `DecideMandate(REVOKE)` landing between the claim and the fence and denying; the mirror ordering still refusing a revocation under a live fence; a destination or identity resolution crossing the fence expiry with zero SES calls; a `SENT` execution whose projection failed being repaired by a worker replay with zero SES calls; a committed approval whose completion write was lost replaying; a sending identity or template version moving before approval being refused; a send over the compiler-invocation boundary with no Core handle; a genuine SES event about another execution being refused; and `SEND_UNKNOWN → SENT` remaining legal with `failure_detail_safe` absent.
- Named tests gain `test_render_precedes_claim_so_sending_can_carry_its_required_hashes`, `test_rendered_hash_mismatch_fails_before_ses`, `test_claim_cas_admits_exactly_one_of_two_workers`, `test_no_ses_call_is_made_from_sending_on_redelivery`, `test_unlisted_ses_exception_classifies_as_send_unknown`, `test_send_unknown_releases_the_fence_and_revocation_proceeds`, `test_reconciliation_rejects_a_disagreeing_message_id`, and `test_send_transaction_participant_counts_are_three_three_and_four`.

## Revisit condition

Revisit § 9's safety property only if SES gains a client-side idempotency token. A stronger claim needs a stronger primitive, not a stronger sentence.

Revisit § 8's classification only by moving a specific observation from the unknown side to the definite side, with the proof that justifies it stated. A change that moves the default is a change that makes ambiguity look resolved.

Revisit § 2's release-on-every-outcome rule only alongside a decision about how a contributor revokes consent on a case with an unresolved send, stated explicitly. A fence that outlives an attempt is a hold on somebody else's authority and needs an owner who accepted that.
