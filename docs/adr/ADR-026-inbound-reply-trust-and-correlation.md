# ADR-026: The trusted inbound reply boundary, reply-to-execution correlation, and the immutable inbound artifact

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § EvidenceItem, § Internal domain events; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Core table mapping, § Shareable table mapping, § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § External reply and commitment creation; [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary, § External reply and verification; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register (T18); [ADR-015](ADR-015-evidence-status-and-verification.md) § Revisit condition; [ADR-020](ADR-020-case-authorization-version.md) § 2; [ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 11 (action case projection participant count)

## Context

### The endpoint that hands the attacker the pen

[08-api-design.md](../architecture/08-api-design.md) § External reply and verification specifies:

```text
POST /v1/demo/external-replies
body {case_id, action_id, channel_message_id, received_at, from_destination_id, subject, text}
```

Every field a reply's provenance depends on is supplied by the caller. `from_destination_id` *asserts* who wrote it, `case_id`/`action_id` *assert* what it answers, and `text` is the body a commitment will be extracted from. Anybody holding the demo token can therefore manufacture a management promise about any case, attributed to the approved destination.

This is the identical defect Phase 8 removed one boundary over. [ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 10 originally described an "operator route" that accepted `{configuration_set, execution_tag, message_id}` as parameters; the repair replaced it with a transport-authenticated event path and the two-half attester/verifier split in `chorus.application.services.ses_events`, because *a message identifier is a value only SES can produce* and a well-formed dictionary is something anybody can type. An external reply is the same class of value and needs the same treatment.

[10-security-threat-model.md](../architecture/10-security-threat-model.md) already names the threat (T18, "malicious email creates commitment/marks resolved") and already concedes "email authenticity beyond the fixed demo destination is out of scope". That concession is what this ADR removes: authenticity is not out of scope, it is *deferred to the transport*, and the boundary that consumes it must be built now so that Phase 11 has one object to satisfy rather than a design to invent.

### There is no channel from an outbound send to an inbound reply

A reply must resolve to exactly one `{namespace, community, case, action, execution}`. Nothing in the repository carries that binding outward:

* `chorus_execution` is an **SES message tag**. Message tags exist for configuration-set event publishing; they are not headers on the delivered MIME, and no recipient ever sees one.
* `reply_to_address` is resolved from the sender's registry secret by `from_identity_id` and is a single fixed address per identity ([ports/sender.py](../../src/chorus/ports/sender.py)). It carries no per-execution component.
* `ActionExecution.ses_message_id` holds the identifier SES issued — but finding an execution *by* that identifier requires an index the persistence document does not have, and [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) forbids scans in request handlers and declares "no GSI is required".

So correlation has to be designed, not discovered.

### `EvidenceItem` cannot express a non-resident author

`EvidenceItem.submitted_by_contributor_id` is a required `ContributorId`. [ADR-015](ADR-015-evidence-status-and-verification.md) anticipated this exactly:

> It must **not** model management as a resident contributor; if the existing required-owner field cannot express a non-resident author, the field, not the truth, is what changes.

## Decision

### 1. The boundary, in four named stages

```text
UNTRUSTED TRANSPORT PAYLOAD          a delivery: mechanism, resource ARN, opaque envelope
        |
        v   InboundMailTransportAuthenticator  (Phase 11 supplies the only implementation)
TRUSTED DELIVERY                     the transport itself proved the origin
        |
        v   InboundMailAttester      verdicts, decode, correlate, attest
AUTHENTICATED CORRELATED ARTIFACT    AttestedInboundReply, minted only here
        |
        v   InboundMailEvidenceVerifier  (held by IngestExternalReply, can only check)
APPLICATION / DOMAIN LOGIC           persistence, extraction, commitment, case state
```

`chorus.ports.inbound_mail` declares two types and nothing else:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class InboundMailTransportContext:
    transport: str  # "aws:ses-receipt", "aws:sns", "aws:sqs" -- as the event source names it
    source_arn: str  # the receipt rule set, topic, or queue the delivery arrived through
    envelope: Mapping[str, Any]  # untrusted; never read before the rest is


class InboundMailTransportAuthenticator(Protocol):
    async def authenticate(self, context: InboundMailTransportContext) -> bool: ...
```

There is deliberately **no `authenticated` flag on the context**. A boolean a caller sets is the forgery, not the defence — the sentence `chorus.ports.ses_events` already carries, restated here because it is the whole reason this port exists.

**Phase 9 ships no deployed authenticator.** Phase 11 owes exactly one object: an `InboundMailTransportAuthenticator` over the SES receipt-rule transport it builds. With no authenticator, the attester refuses everything, `IngestExternalReply` has no verifier to satisfy, and a delivered reply is simply not evidence. A reply that cannot be authenticated producing no commitment is the correct outcome, not a gap.

A **local** authenticator lives in `chorus.infrastructure.local.inbound_mail`, is constructed only by the local/development composition root, and refuses at construction when `settings.environment` is neither `local` nor `development`. A static test asserts the AWS composition root passes `authenticator=None`. This is the same split `chorus.infrastructure.local.sender` already uses, and it is why the local fake does not become a permissive stand-in at the deployed call site.

### 2. `InboundMailAttester.attest` — the frozen order

Authenticate, then gate on verdicts, then decode, then correlate, then attest. Never any other order: decoding first means a forged envelope has already been interpreted by the time anybody asks where it came from.

1. `authenticator is None` → `InboundMailUntrusted(TRANSPORT_UNAVAILABLE)`.
2. `context.transport` or `context.source_arn` differs from the configured pair → `FOREIGN_TRANSPORT_SOURCE`.
3. `authenticate()` returns false → `TRANSPORT_UNAUTHENTICATED`.
4. **Receipt verdicts.** `spfVerdict`, `dkimVerdict`, and `dmarcVerdict` must all be `PASS`; `spamVerdict` and `virusVerdict` must not be `FAIL`. Any other combination → `INBOUND_VERDICT_FAILED`. This step is what turns "who wrote this" from a header into a fact, and it is why a forged `In-Reply-To` gets nowhere: the forger would also have to pass DMARC as the approved destination's domain.
5. **Decode.** Every field is read out of the SES receipt envelope and none is caller-supplied: `mail.commonHeaders.messageId`, `.inReplyTo`, `.references`, `.subject` (read for length only, never persisted), `mail.source` (envelope MAIL FROM), `mail.destination` (envelope RCPT TO), `receipt.action.bucketName`/`objectKey` for the raw MIME, `headersTruncated`. A field missing or shaped differently than SES publishes it makes the whole envelope unusable (`MALFORMED_ENVELOPE`) — an envelope this code had to guess about is an envelope somebody could have constructed.
6. **Correlate** (§ 3). Correlation is inside the attester, not after it, because an artifact that names a case it did not prove it belongs to is exactly the value the rest of the phase must never see.
7. **Attest.** An HMAC over the decoded and correlated fields *and* the `source_arn`, under a key generated inside `inbound_mail_trust_boundary()` and handed to nobody. Constructing `AttestedInboundReply` by hand is possible — Python has no private constructors and pretending otherwise is theatre — and useless, because the attestation on a hand-built instance is a string no verifier reproduces.

`InboundMailEvidenceVerifier.attests()` is a MAC comparison and nothing else. The command that holds it gains no way to mint.

### 3. Correlation: `In-Reply-To`/`References` → the outbound message locator

**The mechanism.** For each msg-id in `inReplyTo` and `references`, take the RFC 5322 local part, and read the immutable **outbound message locator**:

| Item | PK | SK | Mutability |
|---|---|---|---|
| Outbound message locator | `NS#n#EXECUTION#a` | `OUTBOUND_MESSAGE#{sha256(namespace \| ses_message_id)}` | immutable, create-only |

carrying `{namespace, community_id, case_id, action_id, execution_id, destination_id, registry_version, routing_token, sent_at}` and no address, no subject, and no body.

**Amendment to [ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 11.** The locator is written as a **fifth participant of the action case projection**, which therefore moves from four participants to five. It is not added to the send-outcome transaction, because that transaction is run by the sender and the projection is run by the application worker, and it is not written lazily on first reply, because a locator a reply creates is a locator a reply controls. It is additive: no existing participant, field, or condition changes, and `SENT` is the only state that reaches it.

The consequence is deliberate and stated out loud: **a `SEND_UNKNOWN` execution gets no locator, so no reply can attach to it.** An execution the system cannot prove it sent is not an execution a promise can answer.

**The five agreements.** After a locator resolves, all of these must hold or the reply is refused whole:

1. the case, action, and execution load under their own scopes and each item's namespace/community/case re-verifies (`CROSS_CASE_VIOLATION` otherwise);
2. the execution is `SENT` and its `ses_message_id` equals the one the locator names;
3. the case state is `ACTIONED` or `VERIFYING`;
4. `sha256("inbound-reply-party/v1" | namespace | normalize(mail.source))` equals the `address_digest` of the locator's `{destination_id, registry_version, routing_token}` triple;
5. `sha256("inbound-reply-party/v1" | namespace | normalize(mail.destination))` equals the deployment's configured inbound address digest.

**Why digests and not addresses.** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) gives the destination-address secret to the sender alone, and the inbound path must not become a second holder. `address_digest` joins `display_label`, `registry_version`, and `routing_token` in the **non-secret safe destination configuration** the document already describes. No component on this path learns an address, and no audit row, log line, or persisted item has a field to put one in. The residual is stated in § Residual risk.

**Failure outcomes.** All fail closed, none persists anything in the case, and each emits one `reply.rejected` audit event in the namespace partition carrying a closed reason code and no content:

| Situation | Code |
|---|---|
| no `inReplyTo` and no `references` | `REPLY_UNCORRELATED` |
| no msg-id resolves a locator | `REPLY_UNCORRELATED` |
| two msg-ids resolve different executions | `REPLY_CORRELATION_AMBIGUOUS` |
| execution is not `SENT` | `REPLY_EXECUTION_NOT_SENT` |
| `ses_message_id` disagrees with the locator | `REPLY_MESSAGE_ID_MISMATCH` |
| case is `RESOLVED` or `CLOSED_UNRESOLVED` | `REPLY_CASE_TERMINAL` |
| case is in any other state | `REPLY_CASE_NOT_ACTIONED` |
| sender digest ≠ destination digest | `REPLY_SENDER_NOT_DESTINATION` |
| recipient digest ≠ configured inbound digest | `REPLY_RECIPIENT_NOT_OURS` |
| already-processed `messageId` | idempotent replay of the recorded outcome |

`REPLY_CASE_TERMINAL` is the same rule [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Monitor linkage eligibility applies to the Monitor: a terminal case is reopened by an explicit human command and by nothing that arrives on a wire.

### 4. Body extraction, and the reflected-text attack

Extraction is deterministic and happens in the attester, before anything is persisted:

* Only `text/plain` is read — the whole part when the message is `text/plain`, or the `text/plain` alternative of a `multipart/alternative`. **`text/html` is never parsed**, because an HTML-to-text converter is a parser surface on untrusted input.
* No `text/plain` part → `REPLY_NO_PLAIN_TEXT`.
* **Attachments are refused outright.** Any part that is not `text/plain`, `text/html`, or `multipart/*` → `REPLY_ATTACHMENT_PRESENT`, whole reply refused. No attachment metadata is persisted either: a field recording something the system refuses is a field somebody eventually reads.
* Size caps: raw MIME ≤ 256 KiB, extracted plain text ≤ 8 KiB after normalization. Either exceeded → `REPLY_TOO_LARGE`, refused whole, raw bytes not retained.
* `headersTruncated` true → `REPLY_HEADERS_TRUNCATED`. A truncated header set may have dropped the `References` this correlation depends on.
* Normalization is `chorus.application.services.action_grounding.normalize` — NFC, then whitespace-run collapse. One normalizer, already golden-tested.

**Quoted outbound text is removed, deterministically.** The rendered outbound body is reconstructible from the immutable proposal, view, and template version ([ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 3), and the application already regenerates it for the preview. Ingestion regenerates it again and deletes from the reply every normalized line that either begins with `>` or is exactly equal to a normalized line of that outbound body.

This is not tidiness. Without it, a reply that quotes our own message contains our own `requested_deadline` and our own claim text, and a model extracting a commitment could ground it against text **we** wrote and attribute it to management. Removing exactly the lines we sent is deterministic and needs no `On … wrote:` heuristic.

### 5. The immutable inbound artifact

`EvidenceItem` becomes `evidence-item/v2`:

* `submitted_by_contributor_id: ContributorId | None`;
* new `external_source_binding: ExternalSourceBinding | None`;
* **exactly one of the two is set**, enforced in `__post_init__`. A resident upload has an owner and no binding; an inbound reply has a binding and no owner. Neither is ever a lie about the other.
* Readers accept `evidence-item/v1` rows (owner present, binding absent) unchanged; writers emit `/v2`. No stored row is rewritten.

`ExternalSourceBinding` is a frozen create-only value in the private zone:

```text
destination_id, registry_version, routing_token      who, exactly as the registry stood at ingestion
correlated_action_id, correlated_execution_id        what this answers
inbound_message_id_hash                              sha256 of the RFC 5322 Message-ID
sender_address_digest, recipient_address_digest      the two § 3 comparisons, as digests
transport, transport_source_arn                      the wire it arrived on
spf, dkim, dmarc, spam, virus                        the receipt verdicts, as recorded
received_at                                          the transport's timestamp, UTC
correlation_proof                                    the attestation, carried onto the record
schema_version = "external-source-binding/v1"
```

The raw MIME lives in the **private evidence bucket** under the reply prefix, content-addressed, written before the transaction and conferring no authority until one commits ([ADR-018](ADR-018-safe-evidence-and-compile-commit.md) precedent). It is never in DynamoDB: a 256 KiB body against a 400 KiB item limit is reason enough, and every projection that reads a case partition would then be one field away from carrying it. `EvidenceItem.private_object_key`, `sha256`, `byte_length`, and `media_type = "message/rfc822"` describe it; `extracted_text` holds the normalized, quote-stripped plain text as `SensitiveStr`; `extraction_status = COMPLETE`; `malware_scan_status = CLEAN`, justified by the `virusVerdict` gate and the refusal of every non-text part.

The evidence root is content-addressed over the raw MIME with `derivation_kind = ORIGINAL`, so a byte-identical redelivery collapses to the same root by the mechanism that already exists.

**No `Report` and no `Fact` is created by ingestion.** `independent_sources()` counts active, non-duplicate *reports*; a reply that creates none cannot touch `corroboration_source_count`, cannot corroborate a fact, and cannot move readiness. This is asserted by test, not left to follow from the absence of code.

### 6. Persistence: transaction A, seven participants

Core + Audit, run by the application worker composition of the inbound entry point:

1. `EvidenceRoot`, create-only, `NS#n#COMM#c / EVIDENCE_ROOT#{root_sha256}`;
2. `EvidenceRoot` ID locator, create-only, `NS#n#COMM#c / EVIDENCE_ROOT_ID#{root_id}` ([ADR-017](ADR-017-evidence-root-id-locator.md));
3. `EvidenceItem` carrying the binding, create-only, `NS#n#CASE#k / EVIDENCE#e`;
4. guarded case update — **state unchanged**, `version + 1`, `authorization_version + 1`, conditioned on the exact `version` and `state ∈ {ACTIONED, VERIFYING}`;
5. a `ConditionCheck` that no live send fence holds the case, `NS#n#FENCE#k`;
6. the `reply.received` audit event;
7. the completed `INGEST_REPLY` idempotency record, in the `CASE` partition, keyed on `sha256(inbound_message_id)` — and this plan's commit proof.

**New [ADR-020](ADR-020-case-authorization-version.md) § 2 row 13.** Reply ingestion moves **both** counters. Row 12 already reasons that "the new evidence that justifies a reopen bumped the authorization epoch when it landed", so evidence is authorization-sensitive by that table's own logic; and staling an in-flight compile is the conservative direction. Participant 5 exists for the same reason the mandate-decision transaction has one: bumping the epoch underneath a live fence would stale a send at the worst possible instant.

### 7. What this ADR does **not** grant

It adds the immutable authenticated external-source binding [ADR-015](ADR-015-evidence-status-and-verification.md) § Revisit condition asked for, and it **leaves the allowed verification source set empty**. `EvidenceStatus.VERIFIED` remains unreachable in policy/v1. Authentication answers who wrote something; verification answers what it may establish, and the second does not follow from the first. Adding a source is a separate ADR that must state the limit as well as the grant.

## Alternatives considered

- **Keep the caller-supplied `/demo/external-replies` body, gated by the demo token.** Rejected: it is T18 with the attacker handed the pen, and Phase 8 already removed the identical shape one boundary over. The replacement is `POST /v1/demo/external-replies` taking a **fixture selector** — `{fixture_id}` naming a reviewed RFC 822 message in the repository — which the local composition feeds through the *same* attester and the *same* local authenticator. The demo exercises the real boundary rather than bypassing it.
- **A per-execution routing token in `Reply-To` (sub-addressing).** Strictly more robust than threading: it survives a fresh compose, and it does not depend on the recipient's MUA echoing `References`. Rejected for V1 because it makes the letterhead a function of the execution, which reopens the [ADR-023](ADR-023-approval-binding-and-immutability.md) preview binding and the [ADR-025](ADR-025-one-deliberate-ses-attempt.md) frozen SES payload — a Phase-8 contract change to buy robustness against a single controlled demo destination. Recorded as the first candidate should inbound ever face more than one correspondent.
- **A GSI on `ses_message_id`.** Rejected: [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) declares no GSI is required and a new one needs its own ADR; an immutable locator item is cheaper, is create-only, and is written by a principal that already holds the grant.
- **Trust `In-Reply-To` alone, without the sender comparison.** Rejected: message identifiers appear in every copy of a thread, so any party who ever saw the outbound message could forge a reply. The DMARC-verified sender is what makes the identifier a correlation key rather than a credential.
- **Give the inbound Lambda the destination-address secret.** Rejected: it would make a second holder of the addresses the sender exists to be the sole holder of, for a comparison a digest answers.
- **Persist attachment metadata for refused attachments.** Rejected: a recorded field about a refused thing becomes a read field.

## Why chosen

It reuses, unchanged, the boundary Phase 8 built and proved: a transport authenticator port with no implementation until the transport exists, an attester that authenticates before it decodes, a verifier that can only check, and one process-local key neither the API nor any command argument can reach. It makes correlation a durable fact written by the send path rather than a claim made by the reply. It records who wrote a reply without letting that record grant anything. And it fails closed at every one of its closed refusal codes -- four transport, one decode, eight correlation, and four body -- with a quarantined uncertainty rather than an invented certainty.

## Consequences

- New: `chorus.ports.inbound_mail`, `chorus.application.services.inbound_mail`, `chorus.application.commands.ingest_external_reply`, `chorus.infrastructure.local.inbound_mail`, `functions/inbound_mail` as an entry point of the existing worker artifact.
- `EvidenceItem` → `evidence-item/v2`; `ExternalSourceBinding` added to `chorus.domain.entities`; codec accepts v1 and emits v2.
- `IdempotentCommand` gains `INGEST_REPLY`.
- The action case projection ([ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 11) goes from **four** participants to **five**; its arithmetic assertion moves with it.
- `08-api-design.md`'s `/demo/external-replies` body becomes `{fixture_id}`; the six provenance fields are removed.
- `07-action-ses-and-commitments.md` § External reply loses "is an approved destination reply" as a validation bullet — it is now a precondition of the artifact existing at all.
- T18 is rewritten from "fixed destination" to the authenticated-transport boundary. New **T36**: a forged inbound reply attributed to the approved destination. New **T37**: a reply that quotes our own outbound message and is grounded against it.
- The inbound entry point's composition constructs no SES port, no Bedrock client, no compiler client, and no scheduler client; a static test asserts it.
- `SEC-22` is added: *an inbound artifact exists only if an authenticated transport delivered it and it correlated to exactly one `SENT` execution.*

## Residual risk

**`address_digest` is a digest of a low-entropy value.** An attacker who already knows the destination's address can confirm it. That is accepted: the digest is not a secret, it is a comparison token whose purpose is to keep addresses out of the inbound principal, the audit table, and the logs. It is never presented as a credential and never accepted as one.

**Threading depends on the correspondent's mail client.** A manager who composes a fresh message rather than replying produces no `In-Reply-To`, correlates to nothing, and creates no commitment. This is a availability cost paid deliberately for the property that nothing attaches to a case without proof.

**Phase 11 can get the authenticator wrong.** Everything downstream trusts step 3. The mitigation is that step 4's verdict gate and step 3's sender comparison are independent of it: an authenticator that wrongly accepted a foreign delivery would still have to present a DMARC-passing message from the approved destination's domain.

## Revisit condition

Reopen when inbound faces more than one correspondent, when a reply must be accepted without a thread reference, or when Phase 11's transport turns out not to publish SES receipt verdicts on the path it builds. A per-execution `Reply-To` routing token is the named first candidate and requires amending [ADR-023](ADR-023-approval-binding-and-immutability.md) and [ADR-025](ADR-025-one-deliberate-ses-attempt.md) in the same change. Widening the accepted media types, accepting attachments, or parsing `text/html` each require their own ADR stating what parser is being added and what it is being pointed at.
