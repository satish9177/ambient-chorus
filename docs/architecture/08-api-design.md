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
| `POST /demo/clock/advance` | presenter admin | 200 clock + watcher outcome | demo clock enabled; monotonic logical time; same due event ID |
| `POST /ingest/messages` | presenter admin/synthetic adapter | 202 monitor operation | channel-message uniqueness; content-bound key |
| `GET /feed` | presenter/admin | 200 page | namespace/community isolation |
| `GET /operations/{operation_id}` | initiating role/presenter | 200 status | actor/case visibility |
| `GET /session` | any seeded persona | 200 persona binding | actor header resolves the persona; no body |
| `GET /cases/{case_id}` | presenter/approver safe subset | 200 case surface | case membership; approver gets no private data |
| `GET /cases/{case_id}/investigation` | presenter admin | 200 private projection | private role only |
| `POST /cases/{case_id}/mandates` | presenter admin | 200 proposals + case version | case `CANDIDATE`; expected case version; no active send fence |
| `GET /contributors/{contributor_id}/mandates/current?case_id=` | same contributor/presenter | 200 mandate thread | actor=contributor or presenter |
| `POST /cases/{case_id}/mandates/{mandate_id}/decisions` | same contributor | 200 new version | expected current version; no active send fence |
| `POST /cases/{case_id}/investigations` | presenter admin | 202 operation | case in candidate/awaiting/investigating/terminal-reopen flow |
| `POST /cases/{case_id}/views` | presenter admin | 200 ALLOW view or 422 DENY | expected case version; compiler idempotency |
| `POST /cases/{case_id}/actions` | presenter admin | 202 proposal operation | current non-expired view; state ready; matching `authorization_version`; no live `DRAFT` proposal |
| `POST /cases/{case_id}/actions/{action_id}/approvals` | case approver | 200 approval/execution | exact proposal/view/preview hashes; pointer `DRAFT`; execution `DRAFT` at the expected version |
| `POST /cases/{case_id}/actions/{action_id}/invalidation` | case approver | 200 pointer/execution/case | execution `DRAFT`, `APPROVED`, or terminal `FAILED`; refused for `SENDING`, `SENT`, `SEND_UNKNOWN` |
| `POST /cases/{case_id}/actions/{action_id}/executions` | case approver | 202 send operation | matching unexpired approval; execution `APPROVED` at the expected version; safe replay by execution ID |
| `POST /demo/external-replies` | presenter admin | 202 extraction operation | demo only; body is a **fixture selector**, never a reply; the fixture is delivered through the same inbound attester ([ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md)) |
| `POST /cases/{case_id}/commitments/{commitment_id}/verification` | affected contributor | 200 commitment/case | commitment `DUE`; actor owns an `ACTIVE` fact in the case |
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

It rejects a non-`DEMO` namespace, an environment that is not `test`/`development`/`demo`, an active `SENDING` or `SEND_UNKNOWN` execution, an unresolved target prefix, or an unknown seed version. Reset details are in the demo doc.

The frozen response:

```json
{
  "reset_id": "uuid",
  "namespace": "DEMO",
  "seed_version": "elevator/v1",
  "corpus_sha256": "sha256:6a501c33bdac1765fafe17b9f988a4cf06fed5935b95b9a44f1ab9c9621b337d",
  "logical_now": "2030-01-14T09:00:00.000000Z",
  "community_id": "18669fad-d8e1-5995-99fd-296c1ac62a9a",
  "destination_id": "property_manager:demo",
  "contributors": [
    {"actor": "resident_a", "pseudonym": "resident-a", "contributor_id": "c635eb97-..."}
  ],
  "evidence": [{"evidence_id": "uuid", "media_type": "image/jpeg", "sha256": "sha256:..."}],
  "counts": {"deleted": 0, "messages": 24, "contributors": 4, "evidence": 2},
  "replayed": false,
  "audit_event_id": "uuid"
}
```

Every identifier in it is UUIDv5-derived from the seed manifest, so two resets of the same seed return the same values — that is what makes the response usable as the UI's entry point and what makes the reset idempotent in the sense that matters: a repeated reset produces an identical namespace, not merely a second successful call. `replayed` reports whether the `Idempotency-Key` matched a completed reset; a repeat under a *new* key re-runs the delete-and-seed and still lands on the same identifiers.

**Reset seeds, and only seeds.** It creates the community, the four contributors, the logical clock, the 24 messages, the two private evidence objects, the destination registry entry, and the reply fixture catalog. It creates **no** report, fact, candidate case, assessment, mandate proposal or decision, view, action, approval, execution, commitment, schedule, or verification result — those are live outcomes, and a reset that pre-created one would be the demo asserting a result it had not earned. It runs no agent, no compiler, no sender, and no watcher.

`counts.messages` is `24` and `corpus_sha256` is the manifest's declared corpus digest, both verified against the bytes on load. A response whose count or digest disagrees with the manifest is a failed reset, not a reset with a warning.

**Ownership.** Phase 10 owns the reset application service, its local invocation path, and this route against local adapters. Phase 11 owns the deployed version: the durable `DemoManifest` row and `DEMO_RESET_LOCK` in the demo-manifest partition, S3 prefix resolution and bounded deletion, and EventBridge schedule deletion. The service is one implementation with one contract; what changes between the two is which adapters it holds, and a local reset therefore deletes local storage and local objects by exactly the manifest-listed enumeration the deployed one uses — never a scan, never a recursive delete, never a table or bucket drop.

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
case: {case_id,title,state,version,authorization_version,issue_type,corroboration_source_count}
evidence_summary: [{fact_id,safe_label,evidence_status}]
current_shareable_view: ShareableCaseView | null
current_action: ActionProposal + rendered_preview + execution | null   # preview regenerated, never stored
commitments: [CommitmentSafeProjection]
privacy_counts: {included,excluded,denied_by_reason}
```

`current_action` is the Phase-7 section and is served by `ReadCurrentAction`: the immutable proposal's safe fields, the **regenerated** plain-text and HTML preview with the `preview_hash` the proposal committed and whether the two still agree, and the safe `DRAFT` execution projection. Nothing is read back from a persisted body, because ADR-022 § 3 stores neither. A case with no proposal returns `current_action: null`, which is a state rather than an error. Reading a `DRAFT` here is Phase 7 — approving or sending one is Phase 8, and neither verb exists on this surface.

**Phase 10 completes the other five sections**, and completes them as *reads over repository methods that already exist*. No section adds domain state, a persisted projection, a new pointer, or a write path; each is a query and a serializer over a method the persistence layer already exposes. Every field below is either an identifier, a closed enum, a count, a digest, a version, or text that is already externally safe.

| Section | Source | Shape |
|---|---|---|
| `case` | `CoreRepositoryPort.read_case_for_display` | `{case_id, title, issue_type, state, version, authorization_version, corroboration_source_count, state_reason_code}` |
| `evidence_summary` | `CoreRepositoryPort.read_case_facts` | `[{fact_id, fact_type, sensitivity, evidence_status, status, contributor_id, evidence_ids[], version}]` |
| `current_shareable_view` | `load_current_view_pointer` → `load_view` | `ShareableCaseView` (the exact body `POST /views` already returns) or `null` |
| `current_action` | `ReadCurrentAction` | unchanged, plus `execution.version` (§ B below) |
| `commitments` | `ShareableRepositoryPort.read_case_commitments` | `[CommitmentSafeProjection]` |
| `privacy_counts` | current view's compile projection | `{compile_id, included, excluded, denied_by_reason}` |

`case.title` is a **private** field. It is returned to `presenter_admin` and omitted for `case_approver`, which is the rule the paragraph below already states; `state`, the two versions, and the counts are safe for both.

`evidence_summary` carries **no `FactValue`**. It is the fact's identity, type, sensitivity category, resolved `evidence_status`, and owner — enough for `EvidenceStatusList` to render a row and a lock, and never the private text behind it. It is presenter-only for the same reason `case.title` is.

`CommitmentSafeProjection` is frozen as:

```json
{
  "commitment_id": "uuid",
  "action_id": "uuid-or-null",
  "obligor": "Property manager",
  "action_text": "restore elevator B to service",
  "due_at": "2030-01-14T00:00:00.000000Z",
  "verification_method": "resident confirmation",
  "status": "PENDING|DUE|FULFILLED|MISSED|CANCELLED",
  "schedule_generation": 1,
  "version": 3,
  "verified_by_contributor_id": "uuid-or-null",
  "outcome_note": "string-or-null",
  "created_at": "...",
  "updated_at": "..."
}
```

It is every field of `Commitment` except the four that are omitted deliberately — `source_evidence_id` and `due_event_id` are the correlation and replay identities the watcher authenticates against, `scheduler_name` is transport addressing, and `verification_evidence_id` names a private artifact — plus `case_id` and `schema_version`, which the path and the response version already carry. `obligor`, `action_text`, and `verification_method` are already span-cited safe text produced by the deterministic validator under [ADR-027](../adr/ADR-027-commitment-extraction-grounding-and-authority.md), not model prose. `version` is present because `POST .../verification` requires `expected_version`, and a client that cannot read a version cannot submit one.

`privacy_counts` is located without a new pointer. The compile transaction already writes an audit event carrying `AuditEntityRef(entity_type="COMPILER_AUDIT_PROJECTION", entity_id=compile_id)` alongside its `SHAREABLE_VIEW` ref, so the query resolves the current view's `compile_id` from the case's own audit events and then reads `load_compile_projection`. Counts come from `CompileExplanation.included_count` / `.excluded_count`, and `denied_by_reason` is a tally of the exclusion `reason_codes` already on that projection. The per-fact rows behind those counts are **private** and live on the investigation surface below, never here: `ExcludedFactBody` names a private `fact_id` against a denial reason, which is exactly the pairing the safe zone must not carry. `privacy_counts` is `null` when the case has no current view.

For `case_approver`, private title/fact labels and privacy exclusion reasons are omitted; only view/action-safe fields remain. `GET .../investigation` returns reports, private facts, contradictions, root/independence groups, assessment, and per-fact compile inclusion/exclusion explanations to presenter admin. Contradictions are returned structured, each with its cited fact IDs, description, and `materiality`; alternative explanations are returned with their citations; each fact carries its resolved `evidence_status`. It never returns contributor contact or private S3 URI; private evidence uses a separate controlled preview reference.

#### Execution version on the case surface

`current_action.execution` is `{execution_id, state, version}`. The `version` field is added in Phase 10 and it closes a hole rather than adding a feature: `POST .../approvals`, `POST .../invalidation`, and `POST .../executions` all require `expected_execution_version`, and before this field no read returned one. A browser that cannot read the version can only guess it, and a guessed optimistic-concurrency token is the browser deciding that the row it is about to authorize has not moved — which is precisely the decision [11](11-frontend-and-demo.md) forbids it from making. It is the row's existing OCC version, surfaced; it is not a new counter, and it is not the `authorization_version`.

#### Private investigation projection

`GET /v1/cases/{case_id}/investigation`, `presenter_admin` only, is a read over `read_case_facts`, `read_case_reports`, `load_current_assessment`, and `ReadCompileExplanation`. It creates nothing and it is the private half of `PrivacyBoundaryCompare`.

```text
case: {case_id, title, state, version, authorization_version, corroboration_source_count}
reports: [{report_id, contributor_id, evidence_root_id, created_at}]
facts: [{fact_id, fact_type, sensitivity, value_preview, evidence_status, status,
         contributor_id, evidence_ids[], source_message_ids[], version}]
assessment: {assessment_id, based_on_case_version, linkage_decision,
             independent_source_count, is_corroborated, recommended_disposition,
             assessment_hash, created_at,
             findings: [{fact_id, evidence_status, reason_code}],
             contradictions: [{statement_fact_ids[], description, materiality}],
             alternative_explanations: [{description, cited_report_ids[],
                                         cited_fact_ids[], cited_evidence_ids[]}]} | null
compile: {compile_id, decision, based_on_case_version, policy_version, compiler_version,
          view_id, view_hash, reason_codes[],
          facts: [{fact_id, included, granted_scope, reason_codes[],
                   export_fact_ids[], transformation_rule_id}],
          evidence: [{source_evidence_id, included, reason_codes[],
                      export_handle_id, derivative_sha256}]} | null
```

`facts[].value_preview` is the private fact text and it is the one genuinely private payload on this surface. That is the point of the surface: the demo's strongest single moment is a presenter pointing at the mother's health detail and the injected instruction on the left, and their absence from the compiled view on the right. It is served to `presenter_admin` alone, it never appears in the case surface, it never appears in an Action input, and no shareable-zone component may receive it (§ [11](11-frontend-and-demo.md) freezes that boundary in the type system).

`findings[].evidence_status` is the **resolved** status, never the model's proposal: it is the deterministic recomputation against the downgrade-only ladder of [ADR-015](../adr/ADR-015-evidence-status-and-verification.md), and `reason_code` is the single closed code explaining how it got there. No proposed status is persisted, so none is returned; a UI that wanted to show "the agent asked for `VERIFIED` and was refused" has nothing to read, and that is correct — the assessment records what application code decided.

`compile` is `ReadCompileExplanation` verbatim, resolved through the same audit-event ref chain `privacy_counts` uses. It never returns a bucket, a key, a private S3 URI, a contributor contact value, a prompt, or a model completion — the projection type cannot hold any of them.

#### Safe audit page

`GET /v1/cases/{case_id}/audit?limit=100&cursor=...`, `presenter_admin` only, pages `read_case_events` in occurrence order:

```json
{
  "items": [{
    "audit_event_id": "uuid",
    "event_type": "compile.decided",
    "occurred_at": "...",
    "actor_type": "HUMAN|AGENT|SYSTEM",
    "decision": "ALLOW|DENY|NONE",
    "reason_codes": ["SCOPE_INTERNAL_ONLY"],
    "entity_refs": [{"entity_type": "COMPILER_AUDIT_PROJECTION", "entity_id": "uuid", "version": null}],
    "safe_details": {"count": 4, "rule_id": "redact/v1"},
    "correlation_id": "uuid",
    "causation_id": "uuid-or-null",
    "input_hash": "sha256:...",
    "output_hash": "sha256:..."
  }],
  "next_cursor": "string-or-null"
}
```

`actor_id_hash` and `idempotency_key_hash` are omitted. They identify *who* and *which request* rather than *what happened*, they are a correlation channel across personas, and no audit drawer needs them. Everything returned is already a closed code, a bounded count, an identifier, or a digest — `AuditDetails` cannot hold free text, which is what makes "no raw payload leakage" a property of the type rather than a review promise.

#### Persona session binding

`GET /v1/session`, any seeded persona, no body:

```json
{
  "actor": "resident_b",
  "contributor_id": "4b112227-4176-5cfb-bb9a-370d96f4a73a",
  "community_id": "18669fad-d8e1-5995-99fd-296c1ac62a9a",
  "namespace": "DEMO",
  "capabilities": ["DECIDE_MANDATE", "VERIFY_COMMITMENT"]
}
```

`contributor_id` is `null` for `presenter_admin` and `case_approver`, which act as no contributor. This exists because the persona-to-contributor mapping is seeded configuration held only in the composition root: before it, the only way a browser learned a `contributor_id` was the transient `POST /cases/{id}/mandates` response, so a page reload lost the binding and `GET /contributors/{id}/mandates/current` became unaddressable. It is a read of `container.contributor_by_actor` for the *calling* persona only — it resolves who the caller already is and can neither enumerate other personas nor name one. `capabilities` is a closed set derived from the same `require_*` predicates the routes enforce; it is display guidance for the UI, never authorization, and the routes re-decide independently.

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

`POST /v1/cases/{case_id}/actions` body `{expected_case_version,view_id,view_hash}` returns 202 Action operation. The case must be `READY_FOR_ACTION`, its `authorization_version` must equal the view's, and the pointer/hash/expiry must be current. A request arriving while a valid current `DRAFT` proposal stands is 409 with nothing written and no model call.

Proposing uses two idempotency records under one `PROPOSE_ACTION` command family, following the asynchronous investigation's shape: a route/start reservation in the `NAMESPACE` partition binding the key to one operation and one `agent_invocation_id`, and an action-apply commit proof in the `ACTION` partition under a domain-separated key hash. Same key and same request hash returns the same operation and calls no model; a different request hash is 409 `IDEMPOTENCY_CONFLICT` with zero mutations.

`POST .../actions/{action_id}/approvals` body:

```json
{
  "decision": "APPROVED",
  "expected_execution_version": 1,
  "execution_id": "uuid",
  "view_hash": "sha256:...",
  "proposal_hash": "sha256:...",
  "preview_hash": "sha256:..."
}
```

Returns immutable approval and execution `APPROVED`. `preview_hash` is the proposal's immutable preview binding, not the execution's later `rendered_message_hash`; a mismatch is 409. The body carries **no text field of any kind**, so there is nothing in which an edited body could be submitted: an edit is a rejection followed by a new proposal with a new `action_id`, a new `preview_hash`, and a new decision.

A stale browser tab is refused three ways over — an old `proposal_hash`, an old `expected_execution_version`, and an `action_id` the current pointer no longer names — and the transaction repeats all three as participants, so a tab that wins the reads still commits nothing.

`decision: "REJECTED"` records the decision and atomically invalidates the current action pointer, moves the `DRAFT` execution to `FAILED`, and returns the case to `READY_FOR_ACTION` if readiness remains. **A rejection re-checks nothing beyond scope, the pointer, and the execution version**: a human must always be able to say no, including to a proposal that has gone stale ([ADR-023](../adr/ADR-023-approval-binding-and-immutability.md) § 5).

`POST .../actions/{action_id}/invalidation` body `{expected_execution_version, proposal_hash}` is the other path that sets the pointer to `INVALIDATED`. It covers the two cases the approvals route cannot: **withdrawing** an approval whose execution is still `APPROVED`, which races the sender's claim on one row and is resolved by the compare-and-swap; and **clearing** an execution that is already terminal `FAILED`, which is what makes the failure matrix's "create and approve a fresh proposal" remedy reachable after a definite send failure. It refuses `SENDING`, `SENT`, and `SEND_UNKNOWN` with 409.

`POST .../actions/{action_id}/executions` body `{execution_id,expected_execution_version,approval_id}` returns 202. It never accepts recipient, subject, body, claim, attachment, template, or retry flag. Poll operation/case for `SENT|FAILED|SEND_UNKNOWN`. **There is no retry route**, and its absence is a design element: `FAILED` is terminal for an action and `SEND_UNKNOWN` is a quarantine that only reconciliation resolves. Send status is read through the existing case surface's `current_action.execution` and the existing operation poll; no second address for one row is introduced.

### External reply and verification

`POST /v1/demo/external-replies` body is `{"fixture_id": "..."}` **and nothing else**. It names a reviewed RFC 822 message in the repository; the route reads no case, action, destination, sender, subject, or body from the caller, because a caller-supplied reply is not a reply ([ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md) § Context). The fixture is fed through the same `InboundMailAttester` a deployed delivery uses, over the local transport authenticator, so the demo exercises the trust boundary instead of bypassing it. Correlation to exactly one `SENT` execution, the receipt verdicts, the sender/recipient digest comparisons, and every closed refusal code all apply unchanged. On success it stores the immutable inbound artifact and starts an `EXTRACT_COMMITMENT` operation; only the deterministic commitment validator may turn a cited span into a commitment ([ADR-027](../adr/ADR-027-commitment-extraction-grounding-and-authority.md)).

`POST .../commitments/{id}/verification` body `{expected_version,outcome:'FULFILLED'|'MISSED',note?,fixture_evidence_id?}`. The commitment must be `DUE`, and the actor must own an `ACTIVE` fact in the case — a deterministic check against loaded case facts, never a claim in the body. V1 presenter cannot impersonate the response except by selecting the seeded resident persona. `FULFILLED` resolves the case; `MISSED` returns it to `READY_FOR_ACTION` and moves the current action pointer to `INVALIDATED` in the same transaction, so a subsequent action needs a fresh view, proposal, and approval. **This endpoint is the only path by which a commitment is satisfied or missed and the only path by which a case is resolved** ([ADR-027](../adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 8). There is no cancellation route in V1.

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
