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

Agent and send commands must not depend on API Gateway's request timeout. The API conditionally creates an `ApplicationOperation`, invokes a dedicated application-worker Lambda asynchronously, and returns 202. Lambda async delivery may repeat; the operation/input hash and underlying command idempotency make repeats safe. A same-key retry dispatches a pending, undispatched operation but never duplicates a completed one.

`ApplicationOperation` fields: `operation_id`, `kind`, `namespace`, `actor_id_hash`, `case_id?`, `request_hash`, `status: PENDING|RUNNING|SUCCEEDED|FAILED`, `result_refs[]`, `error_code?`, `created_at`, `started_at?`, `completed_at?`, `version`, `expires_at_epoch`. It contains no raw input. The immutable command payload is stored in the appropriate private/safe item referenced by ID. Operation TTL is seven days in demo.

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

`GET /v1/operations/{id}` returns the operation fields plus a typed safe `result` only when succeeded. Private agent output is not returned here; result links point to the appropriate authorized case endpoint. `Retry-After: 1` is returned for pending/running.

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

For `case_approver`, private title/fact labels and privacy exclusion reasons are omitted; only view/action-safe fields remain. `GET .../investigation` returns reports, private facts, contradictions, root/independence groups, assessment, and per-fact compile inclusion/exclusion explanations to presenter admin. It never returns contributor contact or private S3 URI; private evidence uses a separate controlled preview reference.

### Mandate thread and decision

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

`POST /v1/cases/{case_id}/investigations` body `{expected_case_version, reason: INITIAL|NEW_EVIDENCE|REOPEN}`. Returns 202. The worker validates agent output and returns an assessment reference and resulting case state through the operation. An agent recommendation never directly determines the response state.

### Compile view

`POST /v1/cases/{case_id}/views` uses the exact `CompileCommand` minus path-derived namespace/case. An `ALLOW` returns 200 `{decision:'ALLOW',view,included,excluded,audit_event_id}`. A policy denial returns 422 Problem Details with `code=POLICY_DENIED` and structured `reasons`; cross-case returns 403/404 externally and a security audit, stale returns 409. An allowed view is persisted before response.

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
  "instance": "/v1/cases/.../actions/.../executions",
  "correlation_id": "uuid",
  "retryable": false,
  "errors": []
}
```

Details are safe, do not echo input, and do not distinguish a foreign ID from an absent ID to unauthorized callers. Error/status mapping is normative in [09-observability-errors-and-failures.md](09-observability-errors-and-failures.md).
