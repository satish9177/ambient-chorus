# Observability, typed errors, and failure semantics

## Observability goals

Observability must answer: what command ran, which case/version it addressed, which deterministic gate allowed or denied it, whether a replay occurred, what external side effect state exists, and what the operator/user should do next. It must not become a second private corpus.

Every entry point creates or propagates `correlation_id`; nested work adds `causation_id`, W3C `traceparent`, and stable operation/invocation IDs. CloudWatch JSON logs are split by API/worker/compiler/sender/watcher/agent runtime, 14-day demo retention. Audit records are separate business/security artifacts with 90-day demo TTL.

## Structured log schema

Allowlisted fields only:

```text
timestamp              RFC3339 UTC
level                  DEBUG|INFO|WARNING|ERROR|CRITICAL
service                chorus-api|worker|compiler|sender|watcher|monitor-agent|investigator-agent|action-agent
environment            development|demo
event_name             stable dotted name
correlation_id         UUID
causation_id            UUID|null
trace_id / span_id     OTEL IDs
namespace              validated non-secret namespace
actor_type             HUMAN|SYSTEM|AGENT|AWS_SERVICE|null
actor_id_hash          sha256 digest|null
community_id           UUID|null
case_id                UUID|null
case_version           int|null
entity_type / entity_id / entity_version
operation_id / invocation_id / execution_id / commitment_id
input_hash / output_hash / view_hash / proposal_hash
policy_version / prompt_version / template_version
outcome                 SUCCEEDED|DENIED|FAILED|REPLAYED|UNKNOWN
reason_codes            bounded enum array
duration_ms             integer
attempt                 integer
retryable               boolean
aws_request_id          string|null
ses_message_id          string|null (sender-only after accepted)
counts                  typed non-sensitive count object
```

Forbidden everywhere outside explicitly authorized private evidence storage: raw message/report/evidence text, prompts, completions, chain-of-thought, health/unit/name/contact values, email headers/addresses, private/public presigned URLs, S3 keys, mandate terms, rendered email body/subject, access tokens, secrets, and unbounded exception representations. Exception logging emits class, safe code, sanitized dependency/service name, and stack trace only for code frames; SDK request/response bodies are suppressed.

Strands/OpenTelemetry content capture is disabled. An exporter processor drops `gen_ai.prompt`, `gen_ai.completion`, message content, tool arguments/results, and event bodies before export. Agent spans retain model profile hash, prompt version, input/output hashes, token counts, duration, retry, and schema/semantic-validation outcome. A test fails if a sentinel secret appears in captured logs/spans.

## Required events

| Area | Event names and safe evidence |
|---|---|
| ingestion | `message.accepted`, `message.replayed`, `message.conflict`; message ID/hash/count |
| linkage | `candidate.detected`, `report.linked`, `report.link.denied`; case/report IDs, reason codes |
| agents | `agent.invocation.started/completed/failed`, `agent.contract.denied`; agent/prompt/profile hash, IDs, latency/tokens |
| investigation | `investigation.applied`, `evidence.independence.computed`, `contradiction.recorded`, `evidence.status.downgraded`; counts/IDs/statuses. The downgrade event carries the fact ID, the computed status, and the proposed status — codes and identifiers only, never rationale text |
| mandate | `mandate.requested`, `mandate.decided`, `mandate.denied`; case/mandate IDs, version, decision and status codes, granted-fact count, one identity-shared bit, denial reason codes |
| compiler | `compile.started/allowed/denied`, `compile.fact.included/excluded`, `view.persisted`; opaque IDs, rule/reason, scope, hashes |
| action | `proposal.validated/denied`, `approval.recorded/conflict`, `send.fence.acquired/denied/released`; IDs/hashes |
| SES | `execution.sending/sent/failed/unknown/reconciled`; execution ID, safe error, SES ID when known |
| commitment | `commitment.created`, `schedule.created/failed`, `commitment.due/replayed/fulfilled/missed`; IDs/generation |
| security | `cross_case.denied`, `private_uri.denied`, `prompt_injection.observed`, `iam.probe.denied`; no malicious content |
| replay | `idempotency.replay/conflict`, `scheduler.replay`, `lambda.replay`; command/event key hash |
| worker | `worker.job.mismatch`, `operation.resume.scheduled`, `operation.resumed`, `monitor.batch.noop`; operation/invocation IDs, safe reason codes |

### Service attribution

The `service` field names the process that actually emitted the record, not the package the emitter happens to live in. Application events raised while the Monitor operation worker is running carry `worker`; the same emitters called inside an HTTP request carry `chorus-api`. Attributing a worker's agent invocation to the API would make "which process invoked the model" unanswerable from the logs, which is the one question the agent events exist to answer.

`attempt` is the ordinal of the **application-owned** agent attempt within one invocation identity: `1` for the first call and `2` only when the single licensed retry actually happens. It is never the SDK's internal attempt count, because both the Strands event loop and the Bedrock client are pinned to a single attempt.

## CloudWatch metrics and alarms

Use embedded metric format with dimensions limited to `Environment`, `Service`, `AgentName`, `Outcome`, and bounded `ReasonCode`; never case/contributor IDs.

| Metric | Unit / target | Alarm |
|---|---|---|
| `MessagesIngested`, `MessageReplays`, `MessageConflicts` | Count | conflicts >0 warning |
| `CandidateIssuesDetected`, `FalseLinkCorrections` | Count | dashboard/evaluation |
| `AgentInvocations`, `AgentLatencyMs`, `AgentTimeouts`, `AgentContractViolations` | Count/ms | timeout or contract rate >5%/15m |
| `CompilerAllows`, `CompilerDenies`, `CompilerLatencyMs` | Count/ms | errors (not denials) >0 |
| `UnauthorizedExportedFacts` | Count, target **0** | any value critical |
| `PrivacyInvariantViolations` | Count, target **0** | any value critical and sending disabled operationally |
| `CrossCaseDenials`, `StaleAuthorizationDenials` | Count | spike warning |
| `ProposalsValidated`, `ProposalCitationViolations` | Count | violation spike warning |
| `ActionsApproved`, `ActionsSent`, `ActionsFailed`, `ActionsSendUnknown` | Count | any unknown critical/manual reconcile |
| `SendFenceContention`, `ExecutionReplays` | Count | contention >3 warning |
| `CommitmentsCreated/Due/Fulfilled/Missed` | Count | dashboard |
| `ScheduleCreateFailures`, `WatcherReplays` | Count | create failures >0; replay informational |
| AWS `InvocationDroppedCount`, `InvocationsSentToDeadLetterCount` | Count | any >0 critical |
| `DynamoConditionalConflicts`, `OperationAgeSeconds` | Count/seconds | pending operation >120s warning |

The demo dashboard shows the flow by IDs/hashes and a prominent privacy-violations zero counter. Alarms do not include private values in notifications.

## Typed error model

Domain/application code returns/raises closed typed errors; adapters translate SDK exceptions once. API uses safe RFC 9457 responses.

| Error | HTTP | Retryable | Meaning / response action |
|---|---:|---:|---|
| `ValidationError` | 422 (400 malformed JSON) | no | request/schema/invariant invalid |
| `AuthenticationError` / `AuthorizationError` | 401 / 403 | no | invalid access token/actor operation |
| `NotFoundError` | 404 | no | absent or non-enumerated foreign resource |
| `StateTransitionError` | 409 | after reload | illegal source state/guard |
| `PolicyDeniedError` | 422 | only after terms/state change | deterministic disclosure deny with reason codes |
| `StaleAuthorizationError` | 409 | no same artifact | recompile/re-propose/reapprove |
| `SendAuthorizationInProgressError` | 409 | **yes**, after the fence expires (<=60s) | an authorized send holds the case fence; the authorization mutation waits rather than racing it |
| `IdempotencyConflictError` | 409 | no | same key bound to different request |
| `CrossCaseViolationError` | 404 externally; 403 admin | no | whole operation denied and security-audited |
| `AgentContractViolationError` | 502 | no automatic | schema-valid or JSON output semantically invalid |
| `AgentTimeoutError` | 504 | one worker retry | no result persisted |
| `ExternalDependencyError` | 503 | classified | Bedrock/AgentCore/Dynamo/S3/SES definite dependency failure |
| `PersistenceConflictError` | 409 | safe command restart | optimistic condition failed |
| `SchedulerFailureError` | 503 | yes by same schedule identity | commitment visible as unscheduled |
| `SendAmbiguousError` | 409 status projection | **never resend** | execution is `SEND_UNKNOWN`; reconcile only |
| `IntegrityError` | 500 | no; page operator | stored hash/ownership/schema invariant broken |

Policy denial is not logged as an application error. Unknown exceptions become `INTERNAL_ERROR`, correlation ID only, and never echo exception text to the client.

## Complete failure matrix

| Scenario | Expected behavior | Retry / idempotency | Durable state | Audit and user-visible result |
|---|---|---|---|---|
| duplicate ambient message | uniqueness lock finds same channel ID/content hash; return original | exact replay; no Monitor duplicate | one message/report lineage | `message.replayed`; accepted as replay |
| same message delivered twice under new adapter request | content/root hash links duplicate but new channel identity is retained | ingest once per channel ID; downstream duplicate grouping | second message, no new independent source | duplicate badge; not extra corroboration |
| same channel ID with changed content | reject integrity conflict | never overwrite/retry under same key | original only | `message.conflict`; 409 |
| duplicate evidence bytes | reuse `EvidenceRoot`; item may retain new submission lineage | content hash/root conditional | multiple items, one root | duplicate group visible; no independence gain |
| forwarded evidence | collapse parent/forward chain to earliest root; ancestry is resolved through the root-ID locator with bounded direct-key gets | deterministic root traversal; cycles reject; a missing locator or an exceeded traversal bound fails closed | forward relation recorded | marked forwarded; no independence gain |
| duplicate reporter/multiple incidents | unique contributor set counts once for corroboration/privacy where applicable, and one contributor never corroborates a fact against themselves at fact level either | reports remain; no dedupe loss | all reports, one contributor source | UI shows multiple reports/one source |
| duplicate reporter or duplicate root supporting one exact claim | the fact's exact-canonical support group collapses them through the same independence function | deterministic; recomputed on every assessment | fact stays `REPORTED` | no fact-level corroboration gain |
| Bedrock/AgentCore timeout | cancel attempt; retry once with same invocation/input ID if no result | one automatic worker retry; replay result record | no partial agent output; op fails after retry | `agent.timeout`; 504-equivalent operation error |
| AgentCore unavailable | same as transient dependency; backoff within worker budget | one retry, then manual command replay same key | operation FAILED, domain unchanged | `AGENTCORE_UNAVAILABLE`; retry banner |
| runtime exceeds its own budget | the runtime cancels the in-flight model call at `RUNTIME_BUDGET_SECONDS` and answers with a typed runtime error | the application's one licensed retry still applies | no partial agent output | runtime timeout code; the caller never sees a hung invocation |
| storage failure part-way through a frozen apply plan | remaining steps stay pending; `RUNNING→PENDING` | redelivery resumes at the first incomplete step; **zero** model calls | committed steps stand; progress record is authoritative | `operation.resume.scheduled`; operation polls as `PENDING` again |
| case changed externally between two apply steps | the next step's expected version no longer matches the frozen plan | not resumable; never re-planned under the old invocation | committed steps stand and remain valid | `PARTIAL_APPLY_CONFLICT`; operation `FAILED` |
| a job handed to the wrong worker | the worker binds kind, namespace, operation, actor hash, request hash, the operation's own `agent_invocation_id`, and its `agent_binding_hash` before claiming | never claimed, never invoked, never mutated | operation untouched | `worker.job.mismatch` |
| a Monitor apply whose finalization write fails | finalization is the last step of the plan, so the failure is an interruption | `RUNNING→PENDING`; redelivery finishes one step with zero model calls | data steps stay committed | `operation.resume.scheduled` |
| a `SUCCEEDED` transition refused or its response lost | the durable successful invocation record already exists, so the status write is pure transcription | strong reload; conditionally transition to `SUCCEEDED`, fresh claim or stale | never aged into `FAILED` | `operation.resumed` |
| batch with no attributable message | frozen projection is empty of attributable messages; no model call | deterministic; a redelivery reaches the same conclusion | no case, report, fact, or signal | operation `SUCCEEDED` as a no-op with `NO_ATTRIBUTABLE_MESSAGES` |
| projection integrity failure (e.g. an undescribable attachment) | translated to a closed typed application error before any model call | not retryable | domain unchanged | operation `FAILED` with a safe code; never a stranded `RUNNING` |
| invalid agent JSON/schema | reject entire output before semantic use | not automatically retryable | domain unchanged; invocation failure record | `agent.contract.denied`; 502 operation result |
| hallucinated nonexistent fact/evidence ID | semantic validator rejects entire agent output | not retryable without new invocation/prompt fix | domain unchanged | contract violation with ID hash only |
| agent cites fact from another case | deny whole output as cross-case security event | never skip/retry automatically | domain unchanged | `cross_case.denied`; generic failure to UI |
| two similar but different problems | Investigator `DIFFERENT_ISSUES`; deterministic link correction/split candidate | idempotent assessment | no false corroboration; candidate(s) remain | dissimilarity shown; no action readiness |
| insufficient corroboration | readiness guard fails when independent sources <2 | retry only after new evidence | `INVESTIGATING` | reason `CORROBORATION_MIN_NOT_MET` |
| contradiction | store the structured contradiction with citations and materiality; resolve every cited fact to `CONTRADICTED`; do not erase evidence | assessment replay-safe | `LOW` materiality permits readiness and obliges a downstream caveat; `MEDIUM` and `HIGH` block readiness | visible contradiction/caveat; `CONTRADICTION_UNRESOLVED` when blocked |
| model proposes `VERIFIED` | downgraded to the deterministically computed status; policy/v1 has no allowed verification source, so `VERIFIED` is unreachable | not retryable; not a contract violation | assessment persists in full | `EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED` audit; no user-visible failure |
| case version changes during an investigation | the single apply transaction's version condition fails | safe command restart with a fresh read | no assessment, no fact-status change, no transition | `PERSISTENCE_CONFLICT`; 409 stale authorization |
| evidence-root ID locator missing | ancestry cannot be resolved; fail closed rather than under-count | not retryable; page operator | no count, no transition | `INTEGRITY_ERROR`; quotes nothing |
| contributor refuses mandate | append refused version; exclude their facts | exact decision replay; changed request conflicts | case may investigate; compile excludes/denies required | private thread refused; policy reason |
| mandate adjusted/changed | append version, bump case, stale old view/proposal | no mutation in place | old artifacts retained, current pointer changes | UI requires recompile |
| contributor revokes after prior compile | revoke waits for any active send fence; then bumps snapshot | exact revoke replay; no future old export | old view retained historical but stale | send denied if revoke ordered first; revocation visible |
| mandate expires | compiler/send check `now < expires_at`; equality is expired | no same-artifact retry | view/proposal stale; unsent action fails | `MANDATE_EXPIRED`; new mandate required |
| case version changes after compile | proposal/send current pointer check fails | recompile/re-propose | old view immutable, not current | 409 stale authorization |
| policy version changes after approval | send-fence acquisition denies | never send old approval | execution `FAILED/STALE_AUTHORIZATION` | new compile/proposal/approval required |
| concurrent approval/double approve | conditional one-active approval wins | exact key returns winner; other conflicts | one approval/one execution approved | 409 for conflicting decision |
| double-click send/repeated Lambda invoke | state read/CAS permits only one `APPROVED→SENDING` | same execution key returns current result | one execution and at most one SES call | replay audit; UI polls state |
| SES explicit rejection/failure | record definite failure, release fence | no automatic retry; fresh proposal/action/approval | `FAILED` | safe failure code and remediation |
| SES timeout/unknown outcome | quarantine; release fence; do not retry | **never resend**; reconciliation only | `SEND_UNKNOWN` | critical alarm/banner |
| SES accepted but response lost | SES event/tag may positively reconcile | no resend | unknown then `SENT` on proof | reconciliation audit/message ID |
| process dies while `SENDING` | after fence/recovery timeout mark ambiguous unless positive proof | no resend | `SEND_UNKNOWN` | operator reconciliation required |
| scheduler create fails/transient | commitment remains visibly unscheduled; retry same name/token | retryable with deterministic name/client token | commitment + pending schedule projection | scheduler failure banner/alarm |
| scheduler create response lost | `GetSchedule` exact-name/config reconciliation | no second differently named schedule | one generation | replay audit, attach existing ARN |
| scheduler event duplicated / commitment fired twice | watcher event/state conditional returns success no-op | same event ID/generation | one `DUE` transition/request | `watcher.replay`, no duplicate prompt |
| stale scheduler generation fires | watcher rejects/no-ops after verifying generation | never apply | current commitment unchanged | safe replay/stale event audit |
| cross-case requested fact in compile | structural whole compile deny before transformations | not retryable as-is | no view | security audit; 404/403 generic UI |
| private S3 URI accidentally supplied | strict DTO/denylist rejects at boundary; never log or forward | not retryable as-is | no view/proposal | `private_uri.denied`; safe validation message |
| prompt injection in community message | treat as delimited data; Monitor output still validated | ordinary agent retry only on timeout | text remains private; no authority | injection-observed marker, no content in log |
| prompt injection in evidence | Investigator may see; no tools/policy authority; compiler lacks export rule | no policy retry | evidence private, excluded | audit says `UNSAFE_EVIDENCE`/`INTERNAL_ONLY` |
| malicious external reply | bounded untrusted evidence; commitment citations/range/safe-text validation fails | no automatic correction | reply private; no commitment/schedule/action | contract/validation failure visible |
| aggregate below 3 contributors | optional aggregate excluded; required aggregate denies | only after distinct approved contributor added | no under-threshold safe fact | `AGGREGATE_PRIVACY_MIN_NOT_MET` |
| anonymous fact paired with identity request | identity gate strips neither silently nor combines; requested identity excluded/required deny | new mandate if desired | anonymous view only or deny | separate content/identity reasons |
| DynamoDB conditional conflict | abort whole transaction; reload current version | command may retry with fresh expected version; same key safe | no partial transaction | 409 conflict and retry/reload hint |
| DynamoDB transaction outcome unknown | read idempotency/result items before any retry | same transaction/idempotency token semantics | committed once or retried safely | dependency event; user polls operation |
| S3 derivative written but DB compile fails | object remains pending and inaccessible by view | retry same compile reuses hash/key; lifecycle cleans orphan | no view pointer | persistence failure, no export |
| audit write fails in required transaction | domain/view mutation fails with it | safe transaction retry | no unaudited mutation | dependency error |
| log redaction sentinel detected | test/build fails; in runtime emit privacy metric without sentinel value | no automatic continuation for affected release | no domain effect; sending operationally disabled if production-like | critical privacy alarm |

## Red-team operational checks

Before a demo/release, run synthetic canaries that attempt: Action `GetItem` on both DynamoDB tables, private/export `GetObject`, `ses:SendEmail`, and compiler invocation; all must be AccessDenied. A compiler canary tries an internal fact, expired/revoked mandate, aggregate count 2, foreign case ID, and private URI; all must deny. A sender canary uses the SES mailbox simulator or verified demo address and is never run against a real unapproved address.
