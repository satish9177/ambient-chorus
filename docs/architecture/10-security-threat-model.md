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
| T08 | stale view used after revoke/expiry/policy change | unauthorized action | case/snapshot/version/hash/expiry checks and send fence; mandates blocked only during ordered 60s send window | stale-denial metric; residual already-sent mail cannot be recalled |
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
