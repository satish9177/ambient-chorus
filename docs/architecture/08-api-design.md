# V1 API design

## Transport and authorization assumptions

The API is JSON over HTTPS under `/v1`, served by API Gateway HTTP API and FastAPI. JSON bodies are at most 1 MiB; evidence bytes use a separate upload flow only when V1 is expanded—synthetic fixtures are seeded by reset. All responses include `X-Correlation-Id`; clients may supply a UUID, otherwise the API assigns one.

Every deployed request requires `Authorization: Bearer <demo-access-token>`. After token validation, `X-Chorus-Demo-Actor` selects one fixed actor:

- `presenter_admin`: feed, case, investigation, compile, external reply, demo clock/reset;
- `resident_a..resident_d`: only that contributor's mandate and verification decisions;
- `case_approver`: safe preview and approval/execute commands.

This is a hackathon demo access model, not production authentication. Actor IDs come from the seed registry and are never accepted as arbitrary UUIDs. Object-level checks still enforce namespace/community/case/contributor relationships. Responses use `Cache-Control: no-store`; CORS allows the configured web origin only.

Every mutating route requires `Idempotency-Key` (8–128 printable ASCII) except reset, which uses an explicit confirmation field and also accepts a key. Expected versions/hashes are body fields, not weak ETags. Unknown request fields fail validation.

## Asynchronous operation pattern

Agent and send commands must not depend on API Gateway's request timeout. The API creates an `ApplicationOperation` **and completes its command-idempotency record in one transaction**, invokes a dedicated application-worker Lambda asynchronously, and returns 202. A lost transaction response is resolved by reading the record's own commit proof before any retry.

For a route that **mutates before it can create the operation** — `POST /v1/ingest/messages` persists the messages the operation is about, and the operation binds their identifiers — the key is claimed in two phases, and the first phase happens before the first mutation:

1. normalize the request and compute its route request hash;
2. **reserve** the command key: a create-only `IN_PROGRESS` record binding that hash;
3. a record with a *different* hash → `IDEMPOTENCY_CONFLICT` (409) with **zero** mutations;
4. ingest the messages replay-safely;
5. create the `ApplicationOperation` — carrying its agent handover identity — and complete the reservation, in one transaction with a commit proof;
6. dispatch.

The reservation is `IN_PROGRESS` rather than `COMPLETED` so a crash between steps 2 and 5 stays finishable. An `IN_PROGRESS` record under the same key **and the same hash** is this request's own unfinished attempt and is *resumed*, never refused: step 4 is replay-safe by construction, and step 5 is a single transaction, so a key can never name two operations. Refusing an `IN_PROGRESS` record would strand a caller's own identical retry for having once been interrupted.

Because the reservation record already exists before step 5 runs, its mere presence cannot prove that step 5 committed. The commit proof for that transaction therefore names the **version** the completing write moves the record to, and resolution reads that version: still at the reservation's version means the transaction definitely did not commit, and exactly one retry is safe. The answer returned to the caller is then read back from the record, so the durable key-to-operation binding — not a local assumption — decides which operation a racing caller is told about.

Lambda async delivery may repeat; the operation/input hash and underlying command idempotency make repeats safe. A same-key retry that finds a **`PENDING`** operation dispatches the same job identity again, because dispatch itself can fail after the record was written and an undispatched operation would otherwise be stranded forever. Duplicate dispatch is explicitly acceptable: the worker's conditional `PENDING→RUNNING` claim is the duplicate-execution boundary, so however many deliveries arrive, exactly one of them invokes the model. A retry never mints a new `operation_id` or a new `invocation_id`, and a completed operation is never dispatched again.

`ApplicationOperation` fields: `operation_id`, `kind`, `namespace`, `actor_id_hash`, `case_id?`, `request_hash`, `status: PENDING|RUNNING|SUCCEEDED|FAILED`, `result_refs[]`, `error_code?`, `agent_invocation_id?`, `agent_binding_hash?`, `created_at`, `started_at?`, `completed_at?`, `version`, `expires_at_epoch`. It contains no raw input. The immutable command payload is stored in the appropriate private/safe item referenced by ID. Operation TTL is seven days in demo.

`agent_invocation_id` and `agent_binding_hash` are the agent handover identity described in [04](04-domain-state-and-events.md#the-agent-handover-identity): required together for every agent-invoking kind — `MONITOR`, `INVESTIGATE`, and `PROPOSE_ACTION` — before dispatch, `null` together for `SEND_ACTION` and `DEMO_DUE`, immutable for the operation's lifetime, and identifiers and digests only ([ADR-016](../adr/ADR-016-agent-operation-handover-identity.md)). They are **not** part of the public operation status response — a poller is told status and result references, not what the worker binds against.

```json
{
  "operation_id": "uuid",
  "status": "PENDING",
  "poll_url": "/v1/operations/uuid"
}
```

## Endpoint summary

| Method/path | Role | Sync result | State guard / idempotency |
|---|---|---|---|
| `POST /demo/reset` | presenter admin | 200 reset receipt | environment=demo/development, namespace exactly `DEMO`, confirmation; idempotent seed version |
| `POST /demo/clock/advance` | presenter admin | 202 operation | demo clock enabled; monotonic logical time; same due event ID |
| `POST /ingest/messages` | presenter admin/synthetic adapter | 202 monitor operation | channel-message uniqueness; content-bound key |
| `GET /feed` | presenter/admin | 200 page | namespace/community isolation |
| `GET /operations/{operation_id}` | initiating role/presenter | 200 status | actor/case visibility |
| `GET /cases/{case_id}` | presenter/approver safe subset | 200 case surface | case membership; approver gets no private data |
| `GET /cases/{case_id}/investigation` | presenter admin | 200 private projection | private role only |
| `POST /cases/{case_id}/mandates` | presenter admin | 200 proposals + case version | case `CANDIDATE`; expected case version; no active send fence |
| `GET /contributors/{contributor_id}/mandates/current?case_id=` | same contributor/presenter | 200 mandate thread | actor=contributor or presenter |
| `POST /cases/{case_id}/mandates/{mandate_id}/decisions` | same contributor | 200 new version | expected current version; no active send fence |
| `POST /cases/{case_id}/investigations` | presenter admin | 202 operation | case in candidate/awaiting/investigating/terminal-reopen flow |
| `POST /cases/{case_id}/views` | presenter admin | 200 ALLOW view or 422 DENY | expected case version; compiler idempotency |
| `POST /cases/{case_id}/actions` | presenter admin | 202 proposal operation | current non-expired view; state ready |
| `POST /cases/{case_id}/actions/{action_id}/approvals` | case approver | 200 approval/execution | exact proposal/view hashes; state draft |
| `POST /cases/{case_id}/actions/{action_id}/executions` | case approver | 202 send operation | matching unexpired approval; safe replay by execution ID |
| `POST /demo/external-replies` | presenter admin | 202 investigation operation | demo only; same case/action; message uniqueness |
| `POST /cases/{case_id}/commitments/{commitment_id}/verification` | affected contributor | 200 commitment/case | commitment DUE; actor is affected contributor |
| `GET /cases/{case_id}/audit` | presenter admin | 200 page | safe audit only |

These endpoints support exactly the three UI surfaces; route count does not imply extra screens.

## Request and response contracts

### Reset

`POST /v1/demo/reset`

```json
{
  "namespace": "DEMO",
  "confirm": "RESET DEMO",
  "seed_version": "elevator/v1"
}
```

Returns `{reset_id, namespace, seed_version, counts, logical_now, audit_event_id}`. It rejects production, non-DEMO namespaces, active `SENDING` or `SEND_UNKNOWN` executions, unresolved target prefixes, or unknown seed version. Reset details are in the demo doc.

### Ingest messages

`POST /v1/ingest/messages`

```json
{
  "community_id": "uuid",
  "messages": [
    {
      "adapter": "SYNTHETIC",
      "channel_message_id": "feed-001",
      "contributor_id": "uuid-or-null",
      "sent_at": "2030-01-14T08:00:00.000000Z",
      "text": "The lift stopped again this morning.",
      "fixture_attachment_ids": []
    }
  ]
}
```

One to 25 messages, each text <=10,000 characters. Response 202 includes per-message `{channel_message_id,message_id,replay}` and a Monitor operation. Exact redelivery returns existing IDs; same channel ID with different content returns 409 `IDEMPOTENCY_CONFLICT`. The Monitor discovers links; request has no report/case IDs.

### Feed

`GET /v1/feed?community_id={uuid}&limit=50&cursor=...` returns ordered `FeedItem` values `{message_id,sent_at,pseudonym,text,attachment_thumbnails[],chorus_signal?}`. `chorus_signal` contains only `{candidate_case_id,label,related_count,status}`. This presenter surface intentionally displays demo-private raw messages; it is never an Action payload.

### Operation

A `MONITOR` operation may also move `RUNNING→PENDING` when a frozen validated plan was interrupted mid-apply; a client polling it sees `PENDING` again with `Retry-After: 1`, and the resumed worker makes no further model call. `GET /v1/operations/{id}` returns the operation fields plus a typed safe `result` only when succeeded. Private agent output is not returned here; result links point to the appropriate authorized case endpoint. `Retry-After: 1` is returned for pending/running.

### Case surfaces

`GET /v1/cases/{case_id}` returns `CaseSurfaceResponse`:

```text
case: {case_id,title,state,version,issue_type,corroboration_source_count}
evidence_summary: [{fact_id,safe_label,evidence_status}]
current_shareable_view: ShareableCaseView | null
current_action: ActionProposal + rendered_preview + execution | null
commitments: [CommitmentSafeProjection]
privacy_counts: {included,excluded,denied_by_reason}
```

For `case_approver`, private title/fact labels and privacy exclusion reasons are omitted; only view/action-safe fields remain. `GET .../investigation` returns reports, private facts, contradictions, root/independence groups, assessment, and per-fact compile inclusion/exclusion explanations to presenter admin. Contradictions are returned structured, each with its cited fact IDs, description, and `materiality`; alternative explanations are returned with their citations; each fact carries its resolved `evidence_status`. It never returns contributor contact or private S3 URI; private evidence uses a separate controlled preview reference.

### Mandate thread and decision

`POST /v1/cases/{case_id}/mandates` is the human/demo candidate acceptance defined in [ADR-013](../adr/ADR-013-mandate-proposal-endpoint.md). Its body is `{"expected_case_version": 3}` and nothing else; it carries no fact identifier, no grant, and no text. It derives one `PROPOSED` mandate version 1 for every contributor owning an active fact in the case, each grant set to the deterministic least-permissive default for that fact and capped by the policy/v1 maximum (the two are different values; see [ADR-014](../adr/ADR-014-monitor-proposes-no-disclosure-terms.md)), and commits those versions, their current pointers, the `CANDIDATE→AWAITING_MANDATES` transition, the no-live-fence condition, one `mandate.requested` audit event, and its idempotency record in one transaction. It returns `{case_id, case_version, state, proposals:[{mandate_id, version, contributor_id, status, terms_hash, fact_grant_count}]}`. A case that is not `CANDIDATE`, a stale expected version, or a case with no participating contributor is refused with nothing written.


Current mandate response contains proposed/current terms rendered from fact-safe contributor wording, separate content grants, identity grant, destination/purpose, validity, status/version, and revocation history. A contributor sees only mandates they own.

`POST /v1/cases/{case_id}/mandates/{mandate_id}/decisions`:

```json
{
  "expected_version": 1,
  "decision": "APPROVE",
  "fact_grants": [
    {"fact_id": "uuid", "max_scope": "ANONYMOUS_CASE", "allow_safe_transformation": true}
  ],
  "identity_grant": {"externally_shareable": false, "max_scope": "ANONYMOUS_CASE"},
  "expires_at": null
}
```

`decision` is `APPROVE|ADJUST|REFUSE|REVOKE`. Approve must equal proposed terms; adjust supplies complete replacement grants; refuse/revoke cannot include grants. Returns the new immutable mandate version and updated case version. Foreign facts, broad destinations/purposes, or unsupported scopes are 422; stale/current send fence is 409.

### Investigation

`POST /v1/cases/{case_id}/investigations` body `{expected_case_version, reason: INITIAL|NEW_EVIDENCE|REOPEN}`. Returns 202 with an operation to poll. A stale `expected_case_version` is 409 with nothing written. The worker validates agent output and returns an assessment reference and resulting case state through the operation. An agent recommendation never directly determines the response state; readiness is decided by the deterministic predicate in [04](04-domain-state-and-events.md#the-readiness-predicate).

### Compile view

`POST /v1/cases/{case_id}/views` uses the exact `CompileCommand` minus path-derived namespace/case. An `ALLOW` returns 200 `{decision:'ALLOW',view,included,excluded,audit_event_id}`. A policy denial returns 422 Problem Details with `code=POLICY_DENIED` and structured `reasons`; cross-case returns 403/404 externally and a security audit, stale returns 409. An allowed view is persisted before response.

Compile is **synchronous**. It creates no `ApplicationOperation`, and `ApplicationOperationKind` gains no `COMPILE` member.

Compile idempotency uses the ordinary two-part identity, and `compile_id` does not replace it. The `Idempotency-Key` header together with the namespace, actor, and `COMPILE_VIEW` command family identifies the command record; the request hash is computed over the normalized `CompileCommand`, **including `compile_id`**. The same key with the same request hash replays the recorded result; the same key with a different request — a different `compile_id` among them — is `IDEMPOTENCY_CONFLICT` (409). `compile_id` binds the logical compile and addresses its audit projection.

**A denial is a recorded outcome, not an absent one.** A `DENY` persists its audit event, its compiler audit projection, and a completed idempotency record carrying the deterministic denial response, atomically and in the same transaction. A redelivered denied command therefore replays its answer rather than re-running the compile and appending a second record of one decision. A conservative stale denial is safe to record because it grants no authority; a later attempt under changed circumstances is a new command under a new key. A completed logical compile, allowed or denied, is never regenerated on replay.

### Propose, approve, execute

`POST /v1/cases/{case_id}/actions` body `{expected_case_version,view_id,view_hash}` returns 202 Action operation. The case must be `READY_FOR_ACTION`, and the pointer/hash/expiry must be current.

`POST .../actions/{action_id}/approvals` body:

```json
{
  "decision": "APPROVED",
  "expected_execution_version": 1,
  "view_hash": "sha256:...",
  "proposal_hash": "sha256:...",
  "rendered_message_hash": "sha256:..."
}
```

Returns immutable approval and execution `APPROVED`. Reject returns decision and leaves execution `DRAFT`/proposal for rework. Preview hash mismatch is 409.

`POST .../actions/{action_id}/executions` body `{execution_id,expected_execution_version,approval_id}` returns 202. It never accepts recipient, subject, body, claim, attachment, or retry flag. Poll operation/case for `SENT|FAILED|SEND_UNKNOWN`.

### External reply and verification

`POST /v1/demo/external-replies` body `{case_id,action_id,channel_message_id,received_at,from_destination_id,subject,text}`. It stores private evidence and starts an Investigator operation. Only the deterministic commitment validator/scheduler may turn cited terms into a commitment.

`POST .../commitments/{id}/verification` body `{expected_version,outcome:'FULFILLED'|'MISSED',note?,fixture_evidence_id?}`. Actor must own an active fact/report in the case; V1 presenter cannot impersonate the response except by selecting the seeded resident persona. `FULFILLED` resolves; `MISSED` returns case to ready-for-action.

### Audit

`GET /v1/cases/{case_id}/audit?limit=100&cursor=...` returns safe `AuditEvent` projections. Compiler events show fact IDs, scopes, destination, decision/reason, rule IDs, hashes, and inclusion/exclusion without raw values. Example: `{subject_ref:'mother_health_condition fact ID',scope:'INTERNAL_ONLY',destination:'property_manager:demo',decision:'DENY',reason_codes:['SCOPE_INTERNAL_ONLY']}`.

## Error response

Errors follow RFC 9457 Problem Details plus stable fields:

```json
{
  "type": "urn:chorus:error:stale-authorization",
  "title": "Authorization snapshot is stale",
  "status": 409,
  "code": "STALE_AUTHORIZATION",
  "detail": "Recompile and request a new approval.",
  "instance": "/v1/cases/{case_id}/actions/{action_id}/executions",
  "correlation_id": "uuid",
  "retryable": false,
  "errors": []
}
```

`instance` is the web framework's **resolved static route template**, never the raw request URL. A URL path is caller-controlled: an unmatched segment, an operation identifier, or a case identifier written into it would be echoed straight back out of the error handler, and a caller who can choose the path can choose what a 404 body says. When no route matched — an unknown URL, or a path parameter that failed to parse — the field is **omitted entirely** rather than filled in with something the caller wrote. Query values, header values, and exception `detail` strings are never read into it either.

Details are safe, do not echo input, and do not distinguish a foreign ID from an absent ID to unauthorized callers. Error/status mapping is normative in [09-observability-errors-and-failures.md](09-observability-errors-and-failures.md).

### Request validation errors

The web framework's default validation response is **not** used. A framework validation report quotes the rejected input, so for a body containing private community text it would turn a 422 into a disclosure channel. CHORUS installs its own handler for request-validation and malformed-body failures and answers with the same Problem Details shape.

A validation problem may carry only bounded, safe items in `errors`, each of the form `{"code": <safe enum>, "path": <dotted field path>, "category": <safe category>}`. The rejected value itself is never serialized — no `input`, no request body, no message text, no attachment content, no Pydantic representation, and no exception representation. Field paths are built from an **explicit allowlist of declared transport-schema field names** plus bounded array indices. A path segment is never copied out of the validation report because it *looks* safe: the offending key of an unexpected-field error is caller-supplied, and an attacker who names a field `PRIVATE_HEALTH_DETAIL` or `motherLeelaAsthma4B` would have that name echoed back by any syntax- or regex-based test. A segment that is not a known field name is rendered as `?`, at every depth, including nested message and attachment objects. The list of items is capped and the whole response stays bounded regardless of how large the rejected request was. Malformed JSON, `NaN`, `Infinity`, `-Infinity`, an unknown field, and an oversized array all resolve to the same bounded safe response rather than a 500.

The transport-level `401`/`403` responses use the same Problem Details shape and carry no caller-supplied text. An exception that maps to nothing known returns `INTERNAL_ERROR` and the correlation ID, and nothing else.

### Operation idempotency

`POST /v1/ingest/messages` treats its `messages` array as a **batch**, and Monitor processing canonicalizes and sorts it, so operation identity is insensitive to HTTP array order: normalized messages are sorted by `(adapter, channel_message_id)` and each message's attachment descriptors by `evidence_id` before the request hash is computed. `[A,B]` and `[B,A]` are therefore the same command under one key; genuinely different message or attachment content is still a conflict.

`Idempotency-Key` binds the *operation*, not merely the rows a command wrote. Repeating `POST /v1/ingest/messages` with the same namespace, actor, command type, key, and request hash returns the same `ApplicationOperation` — the same `operation_id` and the same `invocation_id` — and mints no new agent execution. The request hash is computed from the authoritative normalized HTTP command content, never from generated identifiers or result ordering, so a replay hashes identically. The same key with a different request hash is `IDEMPOTENCY_CONFLICT` (409). A completed operation replays its recorded result and calls no model.

The ownership boundary sits **above** per-message ingestion, and the ordering is part of the contract, not an implementation detail:

> If `POST /v1/ingest/messages` returns `IDEMPOTENCY_CONFLICT` because the key belongs to another request, **no state derived from the conflicting request exists** — no `CommunityMessage`, no `EvidenceRoot`, no channel uniqueness lock, no feed signal, no operation, and no dispatch.

Per-message idempotency records still exist underneath, and they are what make the conflicting request's *identical* retry cheap; they are not what decides whether the route accepted it.
