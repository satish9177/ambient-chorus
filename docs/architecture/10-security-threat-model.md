# Security and privacy threat model

## Method and assets

This is a V1 STRIDE/privacy review centered on data flow and authorization rather than compliance claims. Highest-value assets are raw community messages, contributor identity/contact, unit and health facts, private evidence bytes/keys, disclosure mandates, safe-view/proposal/approval integrity, SES recipient/body, and the audit trail. Availability matters, but confidentiality and single-send integrity take priority.

Trust boundaries are enumerated in [02-trust-iam-deployment-configuration.md](02-trust-iam-deployment-configuration.md). Every external/agent boundary uses a narrower closed DTO; generic dictionaries and arbitrary metadata do not cross.

## Threat actors and assumptions

- a malicious or careless community-message/evidence author;
- a malicious external reply sender or spoofed fixture request;
- an authenticated demo user selecting the wrong persona;
- an LLM producing adversarial, hallucinated, or schema-abusing output;
- compromised application, agent, compiler, sender, or watcher code/credentials;
- replay, concurrency, and ambiguous AWS network outcomes;
- developer/operator mistakes in IAM, logging, demo reset, or configuration.

V1 assumes AWS account/Region controls and Bedrock/AgentCore/SES services perform as documented; the single-presenter access token is a known non-production identity compromise. Email authenticity beyond the fixed demo destination is out of scope.

## Threat register and controls

| ID | Threat / path | Impact | Preventive controls | Detection / residual |
|---|---|---|---|---|
| T01 | prompt injection says to reveal private facts | private disclosure | text delimited/labeled data; no agent grants policy; deterministic compiler; Action never receives secrets | injection corpus/audit marker; residual agent quality only |
| T02 | Action hallucinates a fact or foreign ID | false/cross-case claim | strict citations to current export IDs; semantic validator; whole proposal reject | contract metric/security audit |
| T03 | private data accidentally added to safe DTO | direct leak | separate models/packages; `extra=forbid`; negative-key/value scanners; serialization tests | privacy metric; code review; residual unknown lexical encoding |
| T04 | Action runtime fetches/exfiltrates data | boundary bypass | zero tools; no DB/S3 IAM; isolated VPC with no NAT/internet and endpoint-only egress; runtime/resource policies; artifact import scan | deployed AccessDenied/network canaries; AgentCore isolation |
| T05 | Action calls SES/compiler/sender | autonomous action/privilege escalation | explicit IAM deny and no tools; inbound policies | CloudTrail/AccessDenied alarm |
| T06 | application with broad private access sends directly | bypass compiler | application explicit SES deny; sender accepts IDs only; compiler fence | IAM assertion/CloudTrail |
| T07 | sender is passed private body/recipient | leak | API contract accepts IDs only; sender reloads safe artifacts; destination registry; deterministic template | request schema tests/log event |
| T08 | stale view used after revoke/expiry/policy change | unauthorized action | case state, `authorization_version`, snapshot, hash, and expiry checks, and the send fence; mandates blocked only during ordered 60s send window. The epoch is the authorization counter and not the OCC row version, so a lifecycle write cannot fake staleness and a real authorization change cannot hide behind one ([ADR-020](../adr/ADR-020-case-authorization-version.md)) | stale-denial metric; residual already-sent mail cannot be recalled |
| T09 | revoke races SES | unclear authorization order | transactional Core fence; revoke checks no active fence; outcome order audited | fence contention alarm; max 60s delay to revoke |
| T10 | duplicate click/Lambda retry sends twice | duplicate external message | one execution; approval consume CAS; `SENDING` quarantine; no retry from unknown | execution replay metric |
| T11 | SES accepts then response times out | duplicate risk if retried | `SEND_UNKNOWN`, never retry, configuration-set reconciliation only | critical alarm/manual review |
| T12 | cross-case ID guessed or joined | data contamination | scoped repository keys/types; batch whole-operation validation; non-enumerating response | cross-case security audit |
| T13 | duplicate/forwarded evidence manufactures corroboration | false case readiness | content roots, forward ancestry, contributor+root independence recomputation | duplicate-group UI/evaluation |
| T14 | one person spams reports to meet aggregate threshold | re-identification/privacy | distinct contributor count separate from report/evidence counts; no V1 exception | compiler reason/metric |
| T15 | named and anonymous grants conflated | identity leak | separate content and identity gates; safe transformations never inherit identity | pairwise compiler tests |
| T16 | safe photo contains EXIF/person/unit/text | visual/metadata leak | fixed checksum scan, decode/re-encode, metadata strip, human checklist, no direct Action bytes | safe derivative hash/review; V1 arbitrary uploads rejected |
| T17 | private S3 URI leaks in model/log/view | location/credential leak | no URI fields, opaque refs, recursive scanners, redacted logs, short URL API only | sentinel tests/private-uri audit |
| T18 | malicious email creates commitment/marks resolved | workflow manipulation | fixed destination, cited term/date validation, range/cap, agent cannot persist, contributor alone verifies fulfillment | invalid-reply audit; sender authenticity limited in demo |
| T19 | scheduler replay/old generation changes state | false follow-up | event/generation/state conditional, deterministic event ID | replay event/metric |
| T20 | audit/logging stores private corpus | secondary breach | allowlisted schema, OTEL content drop, safe errors, sentinel tests, short retention | privacy alarm; operator access controls |
| T21 | demo reset deletes non-demo data | destructive loss | exact environment/namespace/confirmation, manifest, prefix/key validation, reset lock, dedicated role | reset receipt/audit; refuse missing manifest |
| T22 | config points demo at production-like destination | unintended send | startup environment validator; destination allowlist/verified address; production rejected | deployment smoke/config alarm |
| T23 | compromised Monitor/Investigator runtime reads/exfiltrates AWS data | private escalation | no data-plane IAM/tools; inputs only; no NAT/internet; endpoint-only Bedrock/telemetry/artifact egress; distinct artifact/role | IAM/network canaries; they still see explicit private payloads by design |
| T24 | compromised compiler leaks private data into view | central boundary failure | deterministic reviewed code, no LLM/network, closed transformations/output scans, high-value tests | compiler role is high trust; two-person review before production |
| T25 | compromised sender leaks safe/private data | external exfiltration | no Core/private S3; one fixed recipient; one deterministic template; no arbitrary URLs/attachments | SES tag/CloudTrail; safe facts still externally sensitive |
| T26 | access token theft/persona misuse | private UI exposure | high entropy, sessionStorage, short demo lifetime, rotate/revoke, throttling | known residual; production needs real identity ADR |
| T27 | hash/canonicalization disagreement | approval/view substitution or false stale | RFC 8785 single library/test vectors; backend is sole hasher; client displays server hash | golden tests/integrity fail closed |
| T28 | DynamoDB/S3 partial failure creates unauthorized artifact | inconsistency | the compile transaction is the sole authorization commit point; an export object written before it is unreferenced and confers no authority; unknown PUT resolved by `HEAD` and never by a second key; audit mutation atomic ([ADR-018](../adr/ADR-018-safe-evidence-and-compile-commit.md)) | orphan lifecycle/metrics |
| T29 | compromised compiler mutates private Core state it should not reach | silent corruption of cases, facts, mandates | the send fence has its own partition, so the compiler's only Core write grant is scoped to `NS#*#FENCE#*`; the case-version guard is read-only `ConditionCheckItem`; case-partition writes are explicitly denied; no `UpdateItem` and no blanket transaction action are granted ([ADR-019](../adr/ADR-019-send-fence-partition-isolation.md)) | static negative-capability assertions over the synthesized policy; post-deploy case-write canary must return `AccessDenied` |
| T30 | the application's view condition check becomes a view write grant | the application creates or mutates a `ShareableCaseView`, ending the compiler's sole-writer property | the action-proposal transaction holds `dynamodb:ConditionCheckItem` on `NS#*#VIEW_CURRENT#*` and no `PutItem`, `UpdateItem`, or `DeleteItem` on either view prefix; `LeadingKeys` scopes it and `EnclosingOperation` narrows it further where expressible ([ADR-022](../adr/ADR-022-action-draft-preview-and-transaction.md) § 7) | static negative-capability assertions over every synthesized allow statement; post-deploy view-write canary must return `AccessDenied` |
| T31 | an Action proposal persists against a view that stopped being current while the model was answering | an externally sent message authorized by a superseded view | the apply transaction condition-checks the exact `view_id`, `view_hash`, and pointer row version, and the case's exact `version`, `authorization_version`, and `state`; a move fails the transaction whole and no automatic re-invocation follows | mid-flight race test; `proposal.stale_rejected` metric |
| T32 | an approval artifact whose digest stops verifying after a legal write | integrity checks that fail on the happy path get deleted, and approval substitution becomes undetectable | the approval is fully immutable — `consumed_at` is removed and consumption is the execution reaching `SENDING` — and `hash_approval` omits only row bookkeeping, so recomputation is meaningful at any later instant ([ADR-023](../adr/ADR-023-approval-binding-and-immutability.md) § 1) | a test recomputes the digest after every legal later write to the row, of which there are none |
| T33 | a compromised sender rewrites the immutable proposal it is about to send | unapproved content sent with a self-consistent `proposal_hash`, `preview_hash`, and `rendered_message_hash`, defeating the entire approval chain while every check passes | the execution has its own Shareable partition, so the sender's `PutItem` is scoped by `LeadingKeys` to `NS#*#EXECUTION#*` and writes to `ACTION#`, `ACTION_CURRENT#`, `VIEW#`, `VIEW_CURRENT#`, and `CASE#` are denied by `ForAnyValue`; all Core access is denied outright ([ADR-024](../adr/ADR-024-execution-partition-and-sender-boundary.md)) | static negative-capability sweep of every synthesized sender allow; post-deploy proposal-write canary must return `AccessDenied` |
| T34 | an ambiguous send is resolved by resending | a duplicate external message to a real recipient, sent on the system's own judgement about somebody else's correspondence | `SEND_UNKNOWN` is quarantined from every retry path; **no retry route exists**; the safety property is at-most-one deliberate attempt rather than exactly-once delivery, stated as such ([ADR-025](../adr/ADR-025-one-deliberate-ses-attempt.md) § 9) | `SEND_UNKNOWN` automatic retries has a frozen target of **0**; call-count assertions; residual is a possibly-delivered message recorded as unknown |
| T35 | a forged configuration-set event reconciles `SEND_UNKNOWN` to `SENT` | a false record that an unsent message was delivered, or a real message ID silently replaced | reconciliation is never automatic; proof requires the deployment's configuration set, a `chorus_execution` tag equal to the recomputed derivation for that exact execution, and a message ID; a disagreeing message ID is an `IntegrityError` because monotonic presence refuses the rewrite | reconciliation tests over a mismatched tag, a foreign configuration set, and a disagreeing message ID |

## Spoofing, tampering, repudiation, disclosure, denial, elevation summary

- **Spoofing:** demo bearer token and seeded actor registry gate API; destination ID resolves server-side. Production-grade user identity is explicitly absent.
- **Tampering:** hashes, immutable artifacts, KMS/TLS, S3 versioning, typed values, optimistic conditions, and transaction boundaries detect or prevent changes.
- **Repudiation:** immutable decisions, actor hashes, correlation/causation, proposal/view/approval/execution hashes, SES ID/tags, and schedule generation provide an audit chain.
- **Information disclosure:** compile-only safe construction, physical resources, IAM denies, content-free logs, and opaque evidence refs minimize exposure.
- **Denial of service:** body/count/size limits, API throttling, bounded agent tokens/timeouts/retries, case-size caps, and schedule DLQ constrain abuse. V1 does not target hostile public scale.
- **Elevation of privilege:** agents have no tools/data roles, side effects have single deterministic owners, and lower-authority text cannot issue commands.

## Red-team findings incorporated

1. A send-time check alone still allowed a revoke/send race; the design now uses a short transactional authorization fence.
2. A single-table design weakened physical private/shareable isolation; persistence now uses three tables and two buckets.
3. Allowing model-written body text made citation validation incomplete; output is now structured claims rendered by code.
4. Agent access to safe evidence URLs could become a retrieval/exfiltration tool; Action now receives only opaque safe refs/captions.
5. Retrying a timed-out SES call could duplicate mail; ambiguous outcomes are terminal `SEND_UNKNOWN` until positive reconciliation.
6. Depending on a 20-second schedule weakened the demo; a real schedule is created while the same watcher is invoked through a controlled logical clock.
7. Generic trace capture could leak prompts; content capture is explicitly removed before export.
8. Reset-by-prefix without a manifest was too broad; reset now resolves and validates an exact demo manifest before deletion.

## Residual risks accepted for hackathon V1

- The shared demo token/persona selector is not production authentication.
- Monitor/Investigator receive private text; a compromised runtime could expose the explicit payload through the authorized Bedrock model channel despite no data tools or arbitrary internet. Bedrock/AgentCore trust, endpoint isolation, and no-content telemetry are relied upon.
- The compiler is a high-value trusted component; bugs are mitigated by exhaustive/pairwise/property tests, not formal verification.
- Natural-language claim support checks are conservative but not a proof of entailment; mandatory human preview remains.
- Arbitrary user uploads are unsupported; fixed-fixture checksum scanning is not general malware protection.
- SES acceptance/delivery can remain unknown; availability is sacrificed to prevent duplicates.
- Previously sent messages and disclosed safe facts cannot be revoked retroactively.

These risks block a production claim but not the controlled hackathon demo. The risk register assigns owners and triggers.
