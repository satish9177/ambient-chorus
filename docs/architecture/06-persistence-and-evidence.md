# DynamoDB persistence and S3 evidence design

## Decision

Use three on-demand DynamoDB tables divided by trust boundary, not one global single table and not one table per entity:

1. `chorus-core-{environment}`: private community, messages, reports, facts, mandates, investigations, and send fences.
2. `chorus-shareable-{environment}`: immutable safe views, proposals, approvals, executions, commitments, and UI pointers.
3. `chorus-audit-{environment}`: append-only safe audit records.

Each table uses generic string `PK` and `SK`, has server-side encryption, point-in-time recovery outside disposable development, on-demand billing, and streams disabled in V1. No GSI is required by approved V1 access patterns. Physical separation makes least-privilege IAM understandable; item collections retain simple transactional case access.

## Key grammar

Key segments are uppercase ASCII and IDs are lowercase UUIDs. `namespace` is validated `[A-Z][A-Z0-9_]{1,31}`. User text never enters keys.

```text
NS#{namespace}
NS#{namespace}#COMM#{community_id}
NS#{namespace}#CASE#{case_id}
```

All application gets/queries include a namespace prefix. Repositories construct keys from typed IDs and verify the deserialized item's namespace/community/case again. A returned mismatch is `CROSS_CASE_VIOLATION`, not ignored.

## Core table mapping

| Entity/item | PK | SK | Notes |
|---|---|---|---|
| Community | `NS#n` | `COMMUNITY#c` | versioned |
| Contributor | `NS#n#COMM#c` | `CONTRIBUTOR#u` | private contact fields |
| Community message | `NS#n#COMM#c` | `MESSAGE#{sent_at}#{message_id}` | feed query in time order |
| Channel-id uniqueness lock | `NS#n#COMM#c` | `MESSAGE_KEY#{adapter}#{sha256(channel_id)}` | points to message; conditional create |
| Feed signal projection | `NS#n#COMM#c` | `MESSAGE_SIGNAL#{message_id}` | **mutable, versioned** display-only candidate marker written inside a Monitor apply transaction |
| Monitor apply progress | `NS#n#OPERATION#o` | `AGENT_PROGRESS#{invocation_id}` | which apply steps already committed; guarded version |
| Monitor frozen-input snapshot manifest | `NS#n#OPERATION#o` | `MONITOR_INPUT#{invocation_id}#MANIFEST` | digest, byte length, chunk count; immutable create-only |
| Monitor frozen-input snapshot chunk | `NS#n#OPERATION#o` | `MONITOR_INPUT#{invocation_id}#CHUNK#{index:06d}` | ordered canonical UTF-8 slice; immutable create-only |
| Monitor validated-plan snapshot manifest | `NS#n#OPERATION#o` | `MONITOR_PLAN#{invocation_id}#MANIFEST` | digest, byte length, chunk count, bound hashes, provenance digest; immutable create-only |
| Monitor validated-plan snapshot chunk | `NS#n#OPERATION#o` | `MONITOR_PLAN#{invocation_id}#CHUNK#{index:06d}` | ordered canonical UTF-8 slice; immutable create-only |
| Unlinked agent invocation record | `NS#n#OPERATION#o` | `AGENT_INVOCATION#{invocation_id}` | safe hashes/outcome for a Monitor run with no case |
| Application operation | `NS#n#OPERATION#o` | `OPERATION` | status/result refs only; direct poll get; demo TTL |
| Evidence root | `NS#n#COMM#c` | `EVIDENCE_ROOT#{root_sha256}` | dedupe/forward lineage |
| Demo manifest/reset lock | `NS#DEMO` | `DEMO_MANIFEST#{seed_version}` / `DEMO_RESET_LOCK` | exact partition roots, object prefixes, schedules |
| Case root | `NS#n#CASE#k` | `CASE` | aggregate/version/state |
| Report | `NS#n#CASE#k` | `REPORT#r` | private |
| Fact | `NS#n#CASE#k` | `FACT#f` | private |
| Evidence metadata | `NS#n#CASE#k` | `EVIDENCE#e` | private object key |
| Assessment | `NS#n#CASE#k` | `ASSESSMENT#{created_at}#{a}` | immutable |
| Mandate version | `NS#n#CASE#k` | `MANDATE#m#VERSION#{zero-padded-10}` | immutable |
| Mandate current pointer | `NS#n#CASE#k` | `MANDATE_CURRENT#m` | version/status/terms hash/contributor |
| Fact-to-mandate lookup | `NS#n#CASE#k` | `FACT_MANDATE#f#m` | immutable association |
| Agent invocation result | `NS#n#CASE#k` | `AGENT_INVOCATION#i` | input/output hashes and result ref only |
| Send authorization fence | `NS#n#CASE#k` | `SEND_FENCE` | execution ID, snapshot hash, expiry |
| Core idempotency record | contextual partition | `IDEMPOTENCY#{command}#{actor_hash}#{key_hash}` | request hash/result ref/TTL |

Message SK timestamps use canonical UTC strings, allowing `Query PK AND BETWEEN MESSAGE#start AND MESSAGE#end`. Feed pagination returns an opaque base64url JSON cursor containing the last evaluated key, signed with an application HMAC secret; limit 100.

Cases are discovered from messages/candidate results, so V1 has no “list every case” scan. The UI retains case IDs from the feed. A future admin case list may justify one sparse GSI via ADR; scans are forbidden in request handlers.

## Shareable table mapping

Entity-type partition prefixes deliberately support IAM `dynamodb:LeadingKeys`; IAM cannot safely authorize by sort-key prefix.

| Item | PK | SK | Mutability |
|---|---|---|---|
| View | `NS#n#VIEW#v` | `VIEW` | immutable; compiler write only |
| Current view pointer | `NS#n#VIEW_CURRENT#k` | `CURRENT` | compiler conditional replace; `{view_id,hash,case_version,expires_at}` |
| View history index | `NS#n#VIEW_CURRENT#k` | `HISTORY#{generated_at}#{view_id}` | immutable compiler-written safe locator |
| Action proposal | `NS#n#ACTION#a` | `ACTION` | immutable content; application write |
| Approval | `NS#n#ACTION#a` | `APPROVAL#p` | immutable decision; conditional one-time `consumed_at` |
| Execution | `NS#n#ACTION#a` | `EXECUTION#x` | guarded state/version |
| Current action pointer | `NS#n#ACTION_CURRENT#k` | `CURRENT` | application conditional replace/invalidate |
| Action history index | `NS#n#ACTION_CURRENT#k` | `HISTORY#{created_at}#{action_id}` | immutable application-written safe locator |
| Commitment | `NS#n#CASE#k` | `COMMITMENT#c` | guarded state/version |
| Share idempotency | contextual action/case partition | `IDEMPOTENCY#{command}#{actor_hash}#{key_hash}` | immutable request/result, TTL |

Every action/approval/execution endpoint is nested under `/cases/{case_id}` and carries the action ID; the direct action partition read then verifies the item's case ID. Current view/action pointers are direct gets derived from case ID. The scheduler payload carries case and commitment IDs, allowing a query/get in the case commitment partition. A query on the action partition retrieves its proposal/approval/execution lineage; view/action history is a safe locator query under each current-pointer partition. A case UI performs bounded direct pointer gets plus bounded history/action/commitment queries. No GSI is required. Hard limits are 100 active facts, 25 views, 10 actions, and 20 commitments per case; older safe artifacts remain directly addressable but the UI paginates.

## Audit table mapping

| Query | PK | SK |
|---|---|---|
| case audit | `NS#n#CASE#k` | `EVENT#{occurred_at}#{audit_event_id}` |
| namespace events without case (reset/config) | `NS#n` | `EVENT#{occurred_at}#{audit_event_id}` |

Audit cursors follow the feed scheme. Demo audit items have `expires_at_epoch` TTL 90 days after event; TTL is cleanup, never authorization. No raw data is stored. Cross-principal transactions write case mutation plus audit record atomically when both are DynamoDB operations.

## Access patterns

| Use case | Operation | Consistency |
|---|---|---|
| ingest/replay message | transaction: uniqueness lock + message + idempotency | strong conditional |
| read ambient feed | Core query community PK/time SK | eventual; command response is immediate truth |
| read ambient feed signals | Core `BatchGetItem` on the exact `MESSAGE_SIGNAL#{message_id}` keys of the current feed page | eventual; display only, never an authorization input |
| build bounded Monitor context | Core query community PK/time SK for recent messages, then `BatchGetItem` their exact signal keys, then bounded strong `load_case` per distinct case ID | strong for the case summaries |
| load case investigation | Core query case PK; explicit entity filters | strong for commands, eventual for display |
| load current mandate(s) | Core query/get current pointers + immutable versions | strong |
| apply investigation | transaction: assessment, fact statuses, case version, audit | conditional strong |
| compile | strong Core query/batch gets; transaction Share view + current/history pointer partition + Audit | strong |
| propose/approve | strong direct Share view/action/pointer gets; conditional transaction artifacts + audit | strong |
| execute | strong direct Share view/action gets; conditional approval/execution; compiler fence in Core | strong |
| display case/action | direct Share current pointers + bounded action/commitment queries plus authorized Core projection | eventual |
| commitment due | strong Share get by case/commitment; conditional update + audit | strong |

No authorization decision uses a scan, DAX/cache, DynamoDB stream projection, or eventually consistent read.

## Optimistic concurrency and idempotency

Mutable items store `version=N`. Updates use `ConditionExpression version=:expected` and set `N+1`. Create uses `attribute_not_exists(PK) AND attribute_not_exists(SK)`. A conditional failure reloads only enough state to distinguish an exact idempotent replay from `PERSISTENCE_CONFLICT`.

An idempotency record contains `request_hash`, `status: IN_PROGRESS|COMPLETED|FAILED_FINAL`, `result_entity_refs`, `response_status`, `created_at`, and `expires_at_epoch` (24 hours for ordinary demo commands; seven days for send commands). The same key/hash returns the recorded outcome; same key/different hash is `IDEMPOTENCY_CONFLICT`. An expired record does not make an intrinsically unique side effect retryable: sent/unknown execution state remains authoritative forever.

## Transaction boundaries

- **Ingest:** uniqueness lock, message, and idempotency item in one Core transaction. Exact duplicate returns existing message. Same channel ID/different content is an integrity conflict.
- **Monitor apply:** the whole output is semantically validated *before any domain mutation*. Only then is it turned into a deterministic ordered list of bounded apply steps, each committed as its own transaction well under DynamoDB's 100-operation limit at the frozen Monitor maxima. Candidate case creation and that case's initial report linkage are always in one step, so a case never exists without the reports that justified it. Facts are committed in bounded chunks. A durable **apply progress** record under the operation partition advances transactionally with each step, so a retry after a partial delivery detects which steps already committed and resumes only the missing ones — no duplicate report, fact, signal, or audit event. The design permits partial durable *progress* caused by a storage failure; it never permits partial acceptance of an invalid model output, because nothing is written until the whole output has been accepted. An ambiguous transport outcome is resolved by the Phase-2 commit-proof semantics, never by a blind retry.
- **Mandate decision:** append immutable version, update current pointer, bump case version, ensure no live send fence, and append Audit across Core/Audit in one transaction.
- **Investigation:** up to V1's 100-fact case limit, append assessment, update affected fact statuses/case version, and audit in one transaction. Larger output is rejected before persistence.
- **Compile:** safe S3 derivatives are first written with `pending-compile-id`; then one cross-table transaction writes immutable view, current pointer with expected previous hash, idempotency, and audit. Orphan pending objects are lifecycle-deleted after 24 hours.
- **Proposal:** immutable proposal plus its single execution record in `DRAFT`, current pointer, idempotency, and audit in one Share/Audit transaction.
- **Approval:** immutable approval, guarded execution transition `DRAFT→APPROVED`, idempotency, and audit in one Share/Audit transaction.
- **Begin send:** conditionally consume approval and transition execution `APPROVED→SENDING` in one transaction; then acquire Core fence. Stale fence denial transitions to `FAILED` with `STALE_AUTHORIZATION` and no SES call.
- **Commitment:** write commitment `PENDING` and audit, create deterministic schedule outside the transaction, then conditionally attach scheduler name/generation. Creation is retried by name/client token.

## Feed signal projection semantics

The feed signal is a **mutable display projection**, and nothing more. It is explicitly *not* an authorization artifact, *not* the ownership boundary for a `Report`, *not* the authority that decides whether a message or report may join a case, and *not* a permanent one-message-one-case lock. Its storage row must never make otherwise-valid domain state unreachable, so it is written with compare-and-set semantics rather than create-only:

| Stored state | Proposal | Outcome |
|---|---|---|
| absent | any linkage | create |
| identical semantic projection (same case, same version, same display fields) | same linkage | replay; no write |
| same linked case, newer display metadata (label, related count, case state, case version) | same linkage | guarded update at the expected row version |
| **different** linked case | Phase-3 Monitor | **fail before the projection is written** — Phase-3 Monitor cannot relink an already-linked report |
| any | explicit later correction/split use case | may update, under its own authority |

The first row is unconditional, and that matters more than it looks. A case's reports, signals, and facts do not fit in one transaction at the frozen bounds, so a report committed by an apply step and its signal planned for the next step is the ordinary case. A rule that skipped a signal because its report already existed would therefore leave a case member with no marker in the feed and no attempt that would ever write one -- the linkage is durable and correct, and the projection silently is not. A linked message always gets a signal decision; the replay case stays free because a row that already displays exactly this produces no write.

The last two rows are the important distinction. The refusal to relink is a *domain* rule about what Phase-3 Monitor is allowed to do, enforced before any write is staged; it is not a side effect of the row being create-only. A later correction path with the authority to move a report between cases updates this projection like any other display row.

A projection update that fails cannot corrupt domain state: it participates in the same bounded transaction as the reports and facts of its step, so either the step commits whole or none of it does.

The projection exists because the frozen access patterns forbid a scan and require no GSI. Without a signal row in the community partition, resolving "which of these feed rows belong to a discovered pattern" would need exactly the message-to-case index V1 refuses to build. The feed **joins by exact message ID**: a feed page already carries at most 100 message IDs, so the signals for that page are fetched by direct keys through a bounded `BatchGetItem` rather than by independently paginating the signal prefix and hoping the two pages overlap.

## Monitor invocation snapshots

A Monitor operation has **three** distinct durable stages, and the boundary between them is what makes a partially applied operation finishable without ever calling a model again:

1. **frozen input** — the exact bounded `MonitorInput` envelope the model was handed;
2. **validated apply plan** — the whole-output-validated result plus the deterministic ordered step descriptors it produced;
3. **apply progress** — how many of those steps have committed.

The lifecycle is one-directional:

```text
new operation
  -> build the exact bounded MonitorInput once
  -> persist the immutable private input snapshot
  -> invoke the Monitor with that snapshot
  -> whole-output schema and semantic validation
  -> build the deterministic apply plan
  -> persist the immutable private validated-plan snapshot
  -> execute bounded apply steps, advancing progress inside each step's transaction
  -> SUCCEEDED
```

On retry or redelivery the worker reads before it builds:

- **an input snapshot exists** → load it; the context is *never* rebuilt. Same `invocation_id` therefore always means the same `MonitorInput`, and a completed or partially applied invocation can never be reinterpreted against newer context.
- **a validated-plan snapshot exists** → load it; the model is *never* invoked. The invocation is permanently complete the moment that snapshot lands.
- **progress exists** → resume the first incomplete step only.

### Storage shape

Snapshots live only in private Core storage, under the `NS#n#OPERATION#o` partition of the operation that owns them. They are not logs, are not shareable, and are never returned through the API. They may hold structured material derived from private Monitor input and output because Core is the authorized private zone; they hold no provider response envelope, no completion text, and no chain of thought.

A single DynamoDB item cannot exceed 400 KiB and the frozen application payload bound is 1 MiB, so a snapshot is stored as an immutable manifest plus deterministically ordered chunks:

| Property | Rule |
|---|---|
| encoding | RFC 8785 canonical JSON, UTF-8; stored as string attributes, never base64-expanded |
| chunking | fixed maximum chunk size well below 400 KiB, cut only on UTF-8 character boundaries |
| ordering | zero-padded chunk index; the manifest states the exact chunk count |
| integrity | the manifest carries the SHA-256 of the whole canonical byte string; reassembly recomputes it |
| bound | total snapshot bytes stay within the frozen 1 MiB application payload bound |
| mutability | create-only; a snapshot is never updated, only re-created identically |
| scope | namespace, community, operation, and invocation are validated *after* load like every other record |
| corruption | a missing chunk, a wrong chunk count, or a digest mismatch raises `INTEGRITY_ERROR` and quotes nothing |
| access | direct `GetItem`/`BatchGetItem` by exact key only; no scan, no GSI, no prefix walk |
| retention | the demo operation TTL, so a snapshot never outlives the operation it belongs to |

Snapshot creation is itself replay-safe. Manifest and chunks are written in one bounded transaction with create-only conditions; a conditional failure means the snapshot is already there, and the writer proves it is the *same* snapshot by comparing the stored digest with the one it just computed. A stored digest that differs is an idempotency/integrity conflict, never a second opinion.

### What binds a snapshot

The **input snapshot** carries the whole frozen `MonitorInput` — the exact bounded message projection, the pseudonyms used, the attachment descriptors, the candidate case summaries *with the versions the agent was shown*, the allowed issue types, and the declared sensitive-category vocabulary — inside the invocation envelope. `input_hash` is the SHA-256 of exactly those canonical bytes. It is deliberately **not** a hash of the namespace, community, and new-message identifiers alone: that hash could not tell two materially different payloads apart, which is precisely what a redelivery has to be protected from. It is also not a hash of the prompt body; prompt version and model profile remain separate immutable invocation metadata.

The **validated-plan snapshot** carries the validated result envelope and the ordered apply-step descriptors, and binds `input_hash`, `output_hash`, `plan_hash`, `invocation_id`, `operation_id`, the prompt version, the model profile hash, the instant the plan was composed, and the case versions each step expects. Freezing the planning instant matters: every entity timestamp and the audit event's own sort key are derived from it, so a resumed attempt re-stages byte-identical rows instead of appending a second record of one decision.

#### The plan provenance chain

A plan snapshot is the one artifact that authorizes durable writes without re-asking the model, so "it round-tripped" is not a sufficient check. Those seven identifying values form a single `MonitorPlanProvenance` object — `operation_id`, `invocation_id`, `input_hash`, `output_hash`, `plan_hash`, `prompt_version`, `model_profile_hash` — whose digest is stored on the manifest as `provenance_hash` and recomputed from the reassembled document on every load.

That closes the gap left by *duplicated* metadata. The manifest and the document restate the same values, and two copies read out of one partition are not a check: they agree by construction until something rewrites one of them. The chain makes any edit require manifest scalar, document field, document digest, and provenance digest to move together — and even a wholly self-consistent forgery still fails, because the loader independently recomputes two of the values from content and checks three against state it holds itself.

Loading a validated plan therefore proves, in order:

1. the chunks reassemble to the manifest's byte length and content digest;
2. the document's namespace, community, operation, and invocation are the scope it was asked for;
3. manifest and document state the same provenance, and that provenance hashes to `provenance_hash`;
4. `output_hash` recomputed from the stored validated answer equals the bound value;
5. `plan_hash` recomputed from the stored ordered step descriptors equals the bound value;
6. the result envelope's own `invocation_id`, `prompt_version`, and `model_profile_arn_hash` agree with the provenance;
7. `prompt_version` equals the Monitor prompt version *this process runs*, so a plan frozen under different instructions is never applied by a build that would have asked differently;
8. `plan.input_hash` equals the `input_hash` of the frozen input snapshot actually present for this invocation — which is what makes swapping a plan between two invocations, or beside a foreign input, detectable.

Any mismatch raises `INTEGRITY_ERROR`, quotes nothing, applies nothing, and never triggers a model retry or a silent repair. A plan snapshot is never loadable without its frozen input: the input is what step 8 proves against, and a plan whose input is missing is refused outright rather than proved against a rebuilt one.

## Agent invocation records

A `Monitor` run may have no case at all, so the frozen case-partition address is not always available:

- a **case-scoped** agent invocation record lives in the case partition at `AGENT_INVOCATION#{invocation_id}`, as the Core table mapping states;
- an **unlinked** Monitor invocation outcome lives under its `ApplicationOperation` partition at the same sort-key grammar.

Both hold only safe metadata: `invocation_id`, input hash, output hash when one exists, prompt version, model profile hash when available, outcome `SUCCEEDED|FAILED`, a safe failure code, and timestamps/result refs. Neither ever holds raw agent output, prompt text, completion text, or a provider response body. This is a bounded record of specific invocations, not a general agent-event store.

A failed invocation is durable. Invalid schema or a semantic violation leaves the domain unchanged, writes a `FAILED` invocation record with a safe failure code, and fails the operation; a timeout or dependency exhaustion does the same. The record is also what the pre-invocation replay check reads: before any model call, the application strongly reads the record for this `invocation_id`, replays a completed result with a matching input hash without calling the model, and treats a differing input hash as a conflict.

## Cross-case isolation

Case-scoped repositories require `CaseScope(namespace, community_id, case_id)` and verify all loaded/cited items. Batch operations first load all items, then validate the entire set before any transform/write. A single foreign requested fact/evidence/claim citation fails the whole operation and emits `CrossCaseReferenceDenied`; it is never dropped to salvage an action. Random UUIDs reduce guessing but are not the authorization boundary.

## S3 bucket separation

### Private evidence bucket

`chorus-private-evidence-{account}-{region}-{environment}` stores source bytes at:

```text
ns/{namespace}/community/{community_id}/case/{case_id}/evidence/{evidence_id}/v1/original
```

Metadata is allowlisted: `sha256`, `media-type`, `evidence-id`, `case-id`, `root-id`, `uploaded-at`; never resident name, unit, health, message text, or email. The bucket blocks all public access, enforces TLS, uses a private-evidence KMS key, enables versioning, disables ACLs, and denies writes without encryption/checksum tags. Only ingestion/application (write/read) and compiler (read) roles have data access. Action, sender, watcher, web origin, and public principals are explicitly denied.

### Export evidence bucket

`chorus-export-evidence-{account}-{region}-{environment}` stores compiler-created derivatives at:

```text
ns/{namespace}/community/{community_id}/case/{case_id}/view/{view_id}/evidence/{safe_evidence_ref_id}/content
```

It uses a separate KMS key, versioning, block-public-access, and a bucket policy allowing only compiler writes and application-controlled short-lived reads. The Action Agent receives an opaque ref and caption, never a key/URI or presigned URL. The UI requests a 60-second URL from an authorized API endpoint; external email V1 includes no direct attachment unless the safe-ref policy and human preview explicitly allow it.

## Upload, provenance, and duplicate handling

1. Ingestion validates adapter ID, declared size/MIME, magic bytes, and 10 MiB V1 limit before upload.
2. It streams while computing SHA-256; no user-controlled key segment is used.
3. A community evidence-root conditional record on content hash detects exact duplicates. `derived_from`/forward markers collapse to the earliest known root. Duplicate items remain auditable but do not add independent evidence.
4. Upload is tagged `scan=pending`; evidence cannot support export while pending.
5. V1 accepts only fixed synthetic fixtures. `DemoEvidenceScanner` verifies known fixture checksums and MIME/magic bytes. Arbitrary deployed uploads are rejected; a production malware service requires a future ADR.
6. Extraction is bounded and treats all text as untrusted evidence. Text is never executed, interpreted as tool input, or placed in logs.

## Safe photo derivative

The one elevator-error photo follows a narrow, auditable process:

- mandate scope must be `EXTERNAL_ACTION` for the photo;
- fixture checksum/scan must be clean;
- a deterministic image library decodes and re-encodes to PNG, strips EXIF/comments/profiles, caps dimensions, and recomputes SHA-256;
- a fixed demo human review records `NO_FACE`, `NO_UNIT`, `NO_NAME`, `NO_HEALTH`, and a safe caption; review is an input artifact, not an LLM judgment;
- compiler writes the derivative under the current view and includes only its opaque ref/caption.

Text documents and emails are not exportable evidence in V1. The malicious prompt-injection document remains private and is explicitly denied. A decode failure, decompression bomb, unexpected frame, hash mismatch, or review absence fails closed.

## Immutability, retention, and reset

Evidence objects are application-immutable: existing keys may not be overwritten; bucket versioning provides recovery but is not legal WORM/Object Lock. Deletion is allowed only to a dedicated demo reset role constrained to the exact `ns/DEMO/` prefix after namespace verification. Demo private/export objects expire after 30/14 days respectively as backstop lifecycle rules; persisted TTL/lifecycle cleanup never changes whether a historical send was authorized.

`chorus-demo reset` enumerates explicit `DEMO` partition/prefix/schedule targets, validates every resolved key begins with the expected namespace, and batch-deletes only those targets. It refuses reset while any execution is `SENDING` or `SEND_UNKNOWN`. It never deletes a table, bucket, environment root, unresolved variable, or wildcard prefix.
