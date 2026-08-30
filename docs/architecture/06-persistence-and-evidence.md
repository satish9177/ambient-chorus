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
- **Monitor apply:** outputs are validated as a whole, then each proposed report/fact group is applied in a bounded transaction with deterministic IDs and an invocation item. The invocation item records progress so retry completes missing groups without duplicates. Candidate case creation and first report links are atomic.
- **Mandate decision:** append immutable version, update current pointer, bump case version, ensure no live send fence, and append Audit across Core/Audit in one transaction.
- **Investigation:** up to V1's 100-fact case limit, append assessment, update affected fact statuses/case version, and audit in one transaction. Larger output is rejected before persistence.
- **Compile:** safe S3 derivatives are first written with `pending-compile-id`; then one cross-table transaction writes immutable view, current pointer with expected previous hash, idempotency, and audit. Orphan pending objects are lifecycle-deleted after 24 hours.
- **Proposal:** immutable proposal plus its single execution record in `DRAFT`, current pointer, idempotency, and audit in one Share/Audit transaction.
- **Approval:** immutable approval, guarded execution transition `DRAFT→APPROVED`, idempotency, and audit in one Share/Audit transaction.
- **Begin send:** conditionally consume approval and transition execution `APPROVED→SENDING` in one transaction; then acquire Core fence. Stale fence denial transitions to `FAILED` with `STALE_AUTHORIZATION` and no SES call.
- **Commitment:** write commitment `PENDING` and audit, create deterministic schedule outside the transaction, then conditionally attach scheduler name/generation. Creation is retried by name/client token.

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
