# Phased implementation plan

This plan begins only after explicit approval. Each phase is a dependency gate; do not start later work to create the appearance of progress while an earlier security contract is unstable. File paths are targets from [repository structure](../architecture/13-repository-and-coding-standards.md), not claims that files already exist.

## Phase 0 — Repository and toolchain foundation

**Goal:** reproducible Python/web/local/AWS-IaC skeleton with boundary checks and no business behavior.

**Dependencies:** accepted architecture/ADRs.

**Files/modules:** root `pyproject.toml`, locks, `.env.example`, `compose.yaml`, minimal package `__init__`, `apps/web`, `infra/cdk`, test configuration, CI, `.gitignore`.

**Tasks:** configure Python 3.12 uv workspace/groups; Ruff/mypy/pytest/Hypothesis/import-linter; npm/Vite/strict TS/Vitest/Playwright; DynamoDB Local compose; settings schema; safe logging bootstrap; empty CDK stacks; command facade; secret/license scan; architecture-link checker. Generate initial OpenAPI only from a health endpoint if useful, without domain APIs.

**Tests/validation:** tool commands run from clean checkout; invalid/unknown config fails; sensitive logger sentinel is redacted; import-linter demonstrates a forbidden fixture import fails; pinned `npm exec cdk -- --app "uv run python -m infra.cdk.app" synth`; web build/test.

**Exit criteria:** frozen installs, all validation commands documented/green, no secrets, no empty speculative directories, CI equivalent works.

**Risks:** AgentCore/CDK package compatibility and Windows local tooling. Pin direct dependencies and prove synth early.

**Do not implement:** domain entities, real endpoints, AWS deploy, agents, demo outcomes.

## Phase 1 — Domain, state machines, canonicalization, and pure compiler

**Goal:** encode all core invariants and policy/v1 as deterministic, infrastructure-free code.

**Dependencies:** Phase 0.

**Files/modules:** `src/chorus/domain/{ids,time,entities,facts,mandates,state,errors}.py`, `src/chorus/privacy/{policy,compiler,transformations,canonical}.py`, unit/property/golden tests.

**Tasks:** nominal IDs/enums/typed fact union; immutable models; case/action/commitment transition tables; evidence independence; immutable mandate versions/terms hash; compile DTOs/result/reasons; exact 22-gate pipeline; safe transformations except real image I/O; strict Shareable models; RFC 8785 serialization/hash; proposal/approval hash primitives; injected clock/ID generator.

**Tests:** complete transition matrices; required named compiler tests; pairwise scope/identity/destination/purpose/status/expiry; property tests for internal-value nonappearance, aggregate threshold, order determinism, cross-case whole denial; official/captured JCS golden vectors.

**Exit criteria:** pure compiler accepts the synthetic safe example, denies every adversarial example, all security invariants have direct tests, domain imports standard library only.

**Risks:** ambiguous disclosure semantics or canonical serialization. Treat doc conflict as blocker and update ADR/docs before code.

**Do not implement:** DynamoDB/S3, FastAPI, LLM calls, email, generic policy DSL.

## Phase 2 — Persistence ports and DynamoDB adapters

**Goal:** durable, replay-safe entities and transactions with trust-aligned repositories.

**Dependencies:** Phase 1 models/hashes.

**Files/modules:** `src/chorus/ports/{repositories,unit_of_work,clock,ids}.py`, `src/chorus/infrastructure/dynamodb/*`, local/in-memory adapters, persistence integration tests.

**Tasks:** exact key builders/deserializers; Core/Share/Audit repositories; strong/eventual read choice per method; optimistic conditions; idempotency records; cross-table transactions; case/current mandate/view/action pointers; application operation records; send fence primitives; safe pagination cursor signing; typed SDK error mapping; CDK tables/PITR/TTL/encryption.

**Tests:** DynamoDB Local transaction creation/conflict/replay; cross-case/namespace mismatch; no scans; unknown transaction outcome read-before-retry; mandate decision with case bump/no fence; view/current pointer atomicity; approval/execution transitions; audit failure aborts mutation; CDK assertions.

**Exit criteria:** repositories satisfy contract test suite for in-memory and DynamoDB adapters; all authorization reads strong; no GSI/scan; transactions stay below 100 operations at V1 limits.

**Risks:** DynamoDB Local parity and item size/transaction bounds. Test deployed smoke later and enforce model size caps now.

**Do not implement:** agents/API/SES/scheduler; repository-generalization framework.

## Phase 3 — Ambient ingestion and Monitor Agent

**Goal:** ingest/replay the 24-message synthetic feed and discover candidate patterns without fixture IDs.

**Dependencies:** Phase 2 persistence; agent contracts/settings/logging.

**Files/modules:** `src/chorus/contracts/monitor.py`, ingestion/application commands, synthetic adapter/fixture loader, `runtimes/monitor`, AgentCore adapter fake/live, feed routes/queries.

**Tasks:** fixed fixtures/checksums; message uniqueness/content conflict; EvidenceRoot creation for initial items; Monitor input projection/delimiters/prompt v1; Strands structured output; schema and same-input ID/span validation; deterministic durable IDs; report/fact/candidate apply progress; async operation worker; feed signal projection; AgentCore runtime/CDK role.

**Tests:** exact replay/conflict; noise and valid-pattern scenarios; malicious instruction has no authority; malformed/hallucinated Monitor output; timeout/retry invocation record; agent has no tools/data IAM; live synthetic eval gate. The Monitor output schema carries no disclosure-terms field and the pinned prompt version is `monitor/v3` ([ADR-014](../adr/ADR-014-monitor-proposes-no-disclosure-terms.md)); a runtime serving `monitor/v2` is refused by version rather than partially accepted.

**Exit criteria:** from reset messages, live/fake Monitor produces a case candidate through validated contracts, unrelated messages remain noise, replay creates no duplicate reports, UI API can retrieve signal.

**Risks:** model false links/latency. Tune bounded summary/prompt and retain explicit human candidate acceptance; do not hard-code IDs/text rules as the primary detector.

**Do not implement:** Slack/email/WhatsApp, vector DB, embeddings, auto-disclosure, other scenarios.

## Phase 4 — Private mandate workflow

**Goal:** contributor sees exact proposed use and creates immutable approve/adjust/refuse/revoke decisions.

**Dependencies:** candidate/facts from Phase 3; mandate domain/persistence.

**Files/modules:** application mandate commands/queries, API DTO/routes, seed actor/demo access registry, mandate UI surface basic form may wait for Phase 10 but API contracts complete.

**Tasks:** proposed mandates derived from the case's own validated facts, each grant set to the deterministic least-permissive default for that fact and capped by the policy/v1 allowed maximum, created by the candidate-acceptance command in [ADR-013](../adr/ADR-013-mandate-proposal-endpoint.md); contributor ownership checks; fact/identity grants; adjustment validation; immutable version/current pointer/case bump/audit; expiration; send-fence conflict; readiness reconciliation; friendly safe wording separate from raw fact.

The Monitor contributes nothing to a mandate. It has no field in which to name a scope, a purpose, or a set of facts that may travel: [ADR-014](../adr/ADR-014-monitor-proposes-no-disclosure-terms.md) removed `MandateSuggestion` from `MonitorOutput` rather than leaving a scope field in a schema the model is handed while the prose beside it said the field meant nothing. A **policy ceiling is not a proposed grant**: `policy_maximum_scope` caps what any decision may say, `proposed_scope` is what version 1 actually offers, and for a general incident fact those are `EXTERNAL_ACTION` and `ANONYMOUS_CASE` respectively. `APPROVE` authorizes the proposal exactly; reaching the ceiling is an `ADJUST` the fact's owner has to make deliberately.

**Tests:** all decisions/replays/conflicts; foreign fact/contributor; overbroad scope; identity/content pairs; revoke before/after fence ordering; expiry boundary; old view/proposal stale.

**Exit criteria:** A/C/D approval and B adjustment can be performed through API, private sensitive facts remain locked, history is immutable/auditable.

**Risks:** confusing scope UX and accidental overgrant. Defaults are least permissive; policy caps cannot be overridden.

**Do not implement:** standing/general mandates, legal signatures, complex RBAC, notification service.

## Phase 5 — Investigator / Skeptic Agent

**Goal:** validate case sameness, independence, contradictions, evidence status, and readiness without allowing agent state control.

**Dependencies:** reports/facts/evidence/mandates; agent contract/runtime infrastructure.

**Files/modules:** `src/chorus/contracts/investigation.py`, investigation use case/validators, `runtimes/investigator`, API operation, tests/evaluation.

**Tasks:** bounded private case projection; evidence root collapse through the root-ID locator of [ADR-017](../adr/ADR-017-evidence-root-id-locator.md); prompt/data separation; structured output; cited ID/ownership validation; recompute the case-level independent source count and write it to `CommunityCase.corroboration_source_count`; evidence status rules including the verified-source rule of [ADR-015](../adr/ADR-015-evidence-status-and-verification.md); contradictions/alternatives persisted structurally as `investigation-assessment/v2`; assessment persistence in one transaction; case transition guards using the deterministic readiness predicate; the kind-agnostic operation handover of [ADR-016](../adr/ADR-016-agent-operation-handover-identity.md); proposed commitment schema and citation check only (persistence deferred to Phase 9).

Phase 5 **creates no facts**. Contradictions live in the `InvestigationAssessment`, and the apply sets `evidence_status=CONTRADICTED` on affected *existing* facts. The allowed verification source set is empty, so `VERIFIED` is unreachable and every model-proposed `VERIFIED` is downgraded and audited. Fact-level `CORROBORATED` is earned only by independent support for one exact canonical claim and is never inferred from the case-level count.

**Tests:** scenarios 3–7, 13–14; duplicate roots/reporters; forwarded root ancestry through the locator; different issue; contradiction at each materiality; malicious text; invented/foreign IDs; model-proposed `VERIFIED` always downgraded; `proposed_status = CONTRADICTED` without a contradiction entry has no effect; a validated contradiction overrides any proposed status; case corroborated while a unique fact stays `REPORTED`; concurrent case version change proved twice over -- once stale before invocation, where no model is called at all, and once moved *after* the invocation begins, where the model is genuinely asked about version N and only the apply transaction's version condition can refuse the answer; a v1 assessment row's unrecorded contradiction materiality read as `HIGH`; timeout/replay; the compile preflight persists nothing.

**Exit criteria:** case becomes ready only from deterministic sufficiency, contradiction is visible, injection remains data, no agent directly changes state.

**Risks:** investigator overconfidence. Preserve `UNKNOWN/UNCERTAIN`, alternative explanations, and deterministic recalculation; human can close/reopen.

**Do not implement:** knowledge graph/vector retrieval, autonomous case split, web research, commitment scheduling; a `FactType.CONTRADICTION` producer; a mandate re-proposal mechanism; a second current-assessment pointer; any Monitor-style plan or apply-progress snapshot machinery; any `EvidenceItem` provenance field or external-reply storage semantics; any allowed verification source; AgentCore server binding or live AWS evaluation, which stay in Phase 11.

## Phase 6 — Compiler Lambda, safe evidence, and ShareableCaseView

**Goal:** deploy the deterministic boundary and visibly prove private vs external-safe data.

**Dependencies:** pure compiler, current mandates/investigation, persistence, S3 ports.

**Files/modules:** S3 private/export adapters, deterministic image sanitizer/review fixture, `functions/compiler`, compile application adapter/route, IAM/CDK, compiler/audit UI projections.

**Tasks:** strongly load compile state; evaluate pure compiler; strict allowed/excluded results; safe photo re-encode/metadata strip/fixed review under the frozen [ADR-018](../adr/ADR-018-safe-evidence-and-compile-commit.md) profile; content-addressed single-write export object; immutable view/history locator/current pointer/audit/audit-projection transaction; recursive safe-field scanner; hash verify; compiler idempotency including recorded denials; acquire/release send fence operations; bucket/KMS/role definitions and policies.

The safe derivative is written once to its content-addressed key before the transaction, and the **DynamoDB transaction is the sole authorization commit point**; there is no pending object state and no finalization copy. The `ALLOW` transaction has eight fixed participants and the `DENY` transaction three, both independent of how many facts the request named, and both are asserted arithmetically against the staged plan as Phase 5 asserted its own. The compiler adds a check-only `stage_require_case_version` participant rather than reusing the case *update* helper, because its only Core write is the send fence. Ancestry is resolved through the existing [ADR-017](../adr/ADR-017-evidence-root-id-locator.md) `root_closure` service; there is no second traversal. The recursive safe-field scanner is the existing pure privacy one, exposed for reuse if necessary and never re-implemented in infrastructure — `chorus.infrastructure` must not import `chorus.privacy`, so the object-store and sanitizer ports return plain domain values and the application layer assembles `SafeEvidenceCandidate`. No import-linter exception is added.

**Tests:** complete compiler/property suite through adapters; S3 failures, orphans, and unknown-PUT resolution; private URI/metadata/sentinel scans; cross-case whole deny; stale versions; stale compile cannot roll the current pointer backwards; denied compile leaves the current view valid; compile never mutates the Core case; only compiler can write view; Action/others denied buckets/tables by static CDK and IAM assertion; sanitizer determinism across processes; golden view hash and golden derivative hash.

**Exit criteria:** a compile run against local adapters and DynamoDB Local produces the exact safe view containing no secret sentinel, the private audit projection shows every inclusion and exclusion, stale and revoked inputs deny, denials persist without disturbing a valid current view, and the CDK template assertions prove the bucket, key, and role boundaries.

**Deployment ownership.** Phase 6 **owns and synthesizes** the compiler Lambda composition root and artifact code, the S3 adapters, the local and stubbed object-storage proof, the two S3 bucket and separate KMS key definitions, the bucket and key policies, the compiler IAM role and log group, the DynamoDB `LeadingKeys` constraints, the static CDK template and IAM assertions, and the full compiler adapter tests against DynamoDB Local. Phase 6 **does not deploy any of it to AWS**. The deployed compiler Lambda, the live compile, the live safe-evidence object smoke, and the post-deploy IAM canaries belong to Phase 11. This is the same static-now, live-in-Phase-11 split already used for the agent runtimes, and for the same reason: an identity and a policy can be asserted from a synthesized template long before the resource exists, while a canary that proves an `AccessDenied` cannot exist without a deployment.

Synthesizing the compiler role is what made the frozen `W(fence only)` Core statement checkable, and it was not enforceable while the fence shared the case partition. [ADR-019](../adr/ADR-019-send-fence-partition-isolation.md) moved the fence to `NS#n#FENCE#k` — a partition change, not a semantic one — so the sentence became a permission AWS can express.

**Risks:** image library/parser vulnerabilities and compiler centrality. Pillow is pinned exactly, the accepted MIME set, byte, pixel, and dimension caps are frozen, and every rejection fails closed; no arbitrary uploads.

**Do not implement:** general malware/OCR pipeline, public evidence URLs, arbitrary transformations, the time-limited evidence URL endpoint (Phase 10), a `COMPILE` operation kind, or any Action, SES, or commitment behaviour.

## Phase 7 — Action runtime, proposal validator, and preview renderer

**Goal:** generate useful proportionate cited action text from only the safe view.

**Dependencies:** current safe view; public Action contract; application operations.

**Files/modules:** `src/chorus/contracts/action.py`, `runtimes/action`, proposal validator, deterministic renderer, action commands/queries/API, artifact/IAM scans.

**Tasks:** the two case counters and their bump table ([ADR-020](../adr/ADR-020-case-authorization-version.md)); allowlisted Action artifact; tool-less `action/v1` prompt and runtime; the field-for-field safe input mirror; structured proposal with `ActionCaveat` and `ActionTone`; strong current-view and case checks; the frozen grounding grammar ([ADR-021](../adr/ADR-021-action-grounding-and-caveats.md)) covering citation, token support, injection, header, and markup validation; the LOW-contradiction caveat obligation; canonical proposal hash; the `DRAFT` execution presence table; the plain/HTML intermediate renderer and `preview_hash` ([ADR-022](../adr/ADR-022-action-draft-preview-and-transaction.md)); the `VIEW_CURRENT` condition-check port method and its application IAM statement; the ten-participant apply transaction including the case transition to proposed.

Three ADRs were accepted at the Phase-7 freeze gate and each closes a place where implementation would otherwise have invented security-sensitive semantics: [ADR-020](../adr/ADR-020-case-authorization-version.md) separates the OCC row version from the disclosure-authority epoch, so a valid proposal no longer stales the view that authorized it; [ADR-021](../adr/ADR-021-action-grounding-and-caveats.md) makes every model-authored field citation-bound — which removes the factual-premise question rather than answering it — and freezes the exact token grammar behind "lexically supported"; [ADR-022](../adr/ADR-022-action-draft-preview-and-transaction.md) makes a `DRAFT` execution expressible, separates the preview binding from the sent-bytes binding, and enumerates the apply transaction.

**Tests:** valid proposal; hallucinated/foreign/uncited facts; empty citation set on request or caveat; unsupported date/name/number/quotation, and the supported forms accepted; prompt injection in safe fact text; CRLF/HTML/URL/phone/unit; an uncaveated relied-upon contradicted fact; stale view before invocation and a current-view move *during* invocation; a proposal that does not stale its own view; a lifecycle transition that bumps no authorization version; a second proposal against a live `DRAFT`; a lost operation status recovering from the durable invocation record; renderer escaping, one-tree derivation, 100 KiB rejection, and byte/hash goldens; the ten-participant arithmetic assertion; artifact import scan; static CDK/IAM assertions including that the application can condition on and never write a view prefix; evaluation fixtures that **skip when no live binding is configured**.

**Exit criteria:** the captured Action runtime input contains only the serialized view and no private identifier; a valid proposal, its `DRAFT` execution, its pointers, and the case transition persist atomically; an invalid or stale output cannot partially persist; and the synthesized template proves the Action role's denies and the application's read-only view authority.

**Deployment ownership.** Phase 7 **owns and synthesizes** the Action contract, the runtime artifact and its `action/v1` prompt, the AgentCore client adapter, the local fake, the runtime manifest and configuration, the static Action IAM role and log group, the CDK template and IAM assertions, the artifact import scan, the proposal validator, the deterministic renderer, and the action commands, queries, and route. Phase 7 **does not deploy the Action runtime to AWS**. The AgentCore server binding, the deployed runtime resource, the live Action proposal evaluation, and the post-deploy Action IAM and network canaries belong to Phase 11. This is the same static-now, live-in-Phase-11 split already used for the Monitor, the Investigator, and the compiler, and for the same reason: an identity and a policy can be asserted from a synthesized template long before the resource exists, while a canary that proves an `AccessDenied` cannot exist without a deployment.

**Risks:** conservative lexical validator false rejects. The grammar is frozen and fails closed: a false positive rejects and requires a re-proposal, never a bypass, never an override, and never adjudication by a second model. The human preview remains mandatory.

**Do not implement:** free-form bodies, Action tools/repositories, agent-selected recipient, attachments, human approval, SES, a factual-premise classifier, embeddings or semantic similarity, an automatic supersede of a live `DRAFT`, or any AgentCore server binding or live AWS evaluation, which stay in Phase 11.

## Phase 8 — Approval and SES execution

**Goal:** exactly one human-authorized immutable message attempt with explicit ambiguity.

**Dependencies:** proposal/view/hash/renderer; compiler fence; share persistence; SES setup.

**Files/modules:** `approve_action`, `invalidate_action`, `send_action`, `reconcile_send_outcome`, and `project_action_outcome` commands; `functions/sender` and its composition root; SES/destination-registry adapters and local fakes; configuration-set event reconciliation; API routes; `infra/cdk/stacks/sender.py` and its IAM assertions.

**Tasks:** the immutable `approval/v2` with consumption moved onto the execution and `hash_approval`'s omit set narrowed to row bookkeeping ([ADR-023](../adr/ADR-023-approval-binding-and-immutability.md)); `approver_id_hash` + `ApproverAssurance` replacing `approver_id`; approve/reject on exact `proposal_hash`/`view_hash`/`preview_hash` with the execution's `DRAFT@1` compare-and-swap as the one-decision boundary; the invalidation route covering withdrawal and the clearing of a terminal `FAILED`; the `EXECUTION#` Shareable partition and the `IdempotencyPartitionKind.EXECUTION` records that move with it ([ADR-024](../adr/ADR-024-execution-partition-and-sender-boundary.md)); async send operation; render-then-compare-then-claim; the `SendAuthorizationRequest` revalidation Phase 6 deliberately left absent; server-side destination and the `Content.Simple` payload; one SES call and its tag; the classification table with `SEND_UNKNOWN` as the default; fence release on every terminal outcome; recovery and `ReconcileSendOutcome`; the action case projection ([ADR-025](../adr/ADR-025-one-deliberate-ses-attempt.md)).

Three ADRs were accepted at the Phase-8 freeze gate, each closing a place where implementation would otherwise have invented security-sensitive semantics. [ADR-023](../adr/ADR-023-approval-binding-and-immutability.md) makes the approval immutable — a digest that a legitimate write invalidated could not prove anything — states human identity at its real strength, replaces an `attribute_not_exists` condition that could never fire with the compare-and-swap already doing the work, resolves the two contradictory sentences about what rejection does to the case, and adds the invalidation verb without which the failure matrix's own remedy for a definite send failure was unreachable. [ADR-024](../adr/ADR-024-execution-partition-and-sender-boundary.md) finds the [ADR-019](../adr/ADR-019-send-fence-partition-isolation.md) defect a second time: the execution shared a partition with the proposal and the approval, so `W(execution only)` authorized rewriting the message a human approved. [ADR-025](../adr/ADR-025-one-deliberate-ses-attempt.md) fixes an order that could not be executed against [ADR-022](../adr/ADR-022-action-draft-preview-and-transaction.md)'s own presence table, names the compare-and-swap rather than the fence as the duplicate-send boundary, freezes the SES payload field by field, and replaces any exactly-once reading with the at-most-one-attempt property SES can actually support.

The fence checks the case's **state** and its `authorization_version`, and must never require the current `CommunityCase.version` to equal the proposal's recorded `case_version` — lifecycle progression moved that number on purpose, and requiring it would fail every first send ([ADR-020](../adr/ADR-020-case-authorization-version.md) § 6). The same prohibition applies at approval time.

**Tests:** approval immutability, concurrency, and expiry; rejection of a stale proposal; withdrawal racing the claim; invalidation refused for `SENDING`/`SENT`/`SEND_UNKNOWN`; revoke and policy races including authorization-version movement after approval; a rendered-hash mismatch failing before the claim; double click and Lambda replay; SES accepted, explicit fail, unreachable, timeout, and crash; **call count exactly one**; an unlisted exception classifying as unknown; unknown never retries and releases the fence; event reconciliation including a disagreeing message ID; the three participant counts; static IAM assertions that the sender writes only `NS#*#EXECUTION#*`, reads no Core, and that no address-shaped string reaches the synthesized template.

**Exit criteria:** every failure has correct durable state; the ambiguous simulation cannot send again by any path; the audit chain binds `proposal_hash` → `preview_hash` → `rendered_message_hash` with no message text stored; the synthesized sender role proves its denies. **The live verified-destination send is Phase 11**, in the same static-now split the compiler and the three runtimes already use — Phase 8 owns the sender artifact, its role, its log group, and the SES configuration set definition, and deploys none of them.

**Risks:** SES sandbox/domain verification and ambiguous delivery. Complete account prerequisite early; never substitute unsafe retries. The accepted residual is a message that was delivered and recorded as `SEND_UNKNOWN`; the alternative would turn an unknown into a certainty of duplication.

**Do not implement:** recipient lists, CC/BCC, arbitrary templates, campaigns, `Content.Raw`, automatic approval, any retry route, a background reconciliation timer, inbound replies, commitment extraction, or the live AWS deploy — the last belongs to Phase 11 and the four before it to Phase 9.

## Phase 9 — External reply and commitment watcher

**Goal:** turn a cited manager promise into a real schedule and require affected-person verification.

**Dependencies:** action sent path, Investigator proposed commitment, scheduler/persistence ports.

**Files/modules:** external reply ingestion, commitment validator/service, Scheduler adapter, `functions/commitment_watcher`, verification API, demo clock, DLQ/alarms.

**Tasks:** ingest fixed RFC822 reply as private evidence; Investigator operation; validate obligor/action/due/citation/range/safety; commitment state; deterministic schedule/client token/config; response-loss reconciliation; due-event generation/idempotency; watcher DUE transition; fulfilled/missed human guard; case resolution/readiness; logical/actual clock mapping.

Commitment validation needs to know which approved destination authored a reply, so this phase introduces the **immutable authenticated external-source binding**: the destination ID, registry version, and routing token as they stood at ingestion, recorded durably on the stored evidence. Management must not be modelled as a resident contributor; if the required-owner field cannot express a non-resident author, that field is what changes. Adding this binding does **not** make the source eligible to grant `EvidenceStatus.VERIFIED` — authentication answers who wrote something, verification answers what it may establish. Adding an allowed verification source is a separate explicit ADR that must state which claim the source may verify and where that permission stops ([ADR-015](../adr/ADR-015-evidence-status-and-verification.md) § Revisit condition).

**Tests:** malicious reply/date; duplicate root/commitment; schedule explicit/lost response; duplicate/stale generation; DLQ config; due twice; unauthorized verifier; actioned direct-to-resolved illegal; fulfilled/missed transitions; real schedule smoke plus demo clock path.

**Exit criteria:** live reply creates one real future schedule; demo advance invokes same watcher; one verification request; missed returns ready, fulfilled alone resolves.

**Risks:** timezone/date extraction and scheduler eventual behavior. Explicit timezone conversion/range/citations and deterministic demo trigger.

**Do not implement:** recurring calendars, conversational reminder agent, automatic resolution, Step Functions.

## Phase 10 — Complete three-surface frontend

**Goal:** polished, accessible proof of discovery, private mandates, compile boundary, action, and commitment.

**Dependencies:** stable API/OpenAPI for all flows.

**Files/modules:** `apps/web/src/{api,components,surfaces,styles}`, generated schema, UI tests/Playwright.

**Tasks:** token/persona session; feed timeline/signals; mandate form/history; case stepper/evidence/contradiction; always-obvious private vs shareable compare; privacy table; deterministic preview/approval/execution; commitment/verification; operation polling; stale/unknown/error states; responsive/accessibility.

**Tests:** component query/error/loading states; no dangerous HTML; exact version/hash submission; accessibility; Playwright three-surface flow and sentinel absence from safe DOM/network.

**Exit criteria:** a presenter can complete the five-minute path without console/CLI after reset; no fourth product surface; keyboard/contrast/error handling verified.

**Risks:** visual scope creep and polling timing. Reuse small components and fixed script; polish boundary comparison first.

**Do not implement:** dashboard suite, admin/settings/users, mobile app, realtime websockets, design-system package.

## Phase 11 — AWS deployment and AgentCore hardening

**Goal:** reproducible demo stack with least-privilege resources, observability, and rollback.

**Dependencies:** all functional adapters/runtimes; CDK foundation.

**Files/modules:** complete CDK stacks/config, deployment smoke scripts/CLI, dashboards/alarms, runbook additions.

**Tasks:** isolated two-subnet VPC/no NAT and endpoint policies; data/KMS/S3; direct-code AgentCore runtimes/endpoints/profiles/MMDSv2; API/worker/compiler/sender/watcher; API token secret; SES/config events; scheduler/DLQ; CloudFront/S3 web; log/OTEL content filtering; budgets/tags; runtime version outputs; rollback aliases; IAM/network post-deploy canaries.

Every agent runtime's **server binding, deployed resource, and live evaluation** belong here, including the Investigator's. Earlier phases build and unit-test a runtime artifact, its pinned prompt, its adapter, its local fake, its manifest, and its static CDK/IAM assertions; the deployed AgentCore resource and the gated live model evaluation are this phase's deliverable for all three agents alike.

The **compiler boundary follows the same split**. Phase 6 owns the compiler Lambda code, the S3 adapters, and the bucket, KMS key, role, policy, and `LeadingKeys` definitions together with their static template assertions; this phase owns the actual deployment of the compiler Lambda and the S3 and KMS resources into the target account, the first live compiler invocation, the live safe-evidence object smoke, and the post-deploy compiler IAM canaries. A resource defined in Phase 6 and deployed here is one decision recorded once, not a decision made twice.

**Tests:** synth/assertions; clean-account deploy; smoke each endpoint/runtime; canaries; encryption/public access; model/profile lifecycle; CloudWatch dashboards; rollback exercise; no trace content.

**Exit criteria:** one command/set of documented CDK commands deploys reproducibly, all canaries and real-service smoke tests pass, alarms/dashboards work, reset limited to DEMO.

**Risks:** service quotas/regions/account bootstrap/cost. Preflight early, remain in one region, document exact prerequisites; no silent local fallback in deployed demo.

**Do not implement:** multi-region/HA, production account topology, AgentCore Memory/Gateway, autoscaling customizations.

## Phase 12 — End-to-end and adversarial evaluation

**Goal:** prove frozen success/security metrics across local and deployed paths.

**Dependencies:** deployed complete system and fixtures.

**Files/modules:** `demo/evaluation`, evaluation runner/report, expanded integration/E2E/IAM tests.

**Tasks:** run all 15 scenarios three live model times; measure linking/action metrics; run all privacy/idempotency/race/sentinel tests; concurrency/failure injection; cross-case corpus; stale/revoke/unknown; reset/replay; inspect CloudWatch/CloudTrail/artifacts/DOM; fix code/docs/ADRs if architecture changes.

**Tests:** the phase is the full matrix; capture command evidence and safe report.

**Exit criteria:** binary gates zero, quality thresholds met, no unresolved critical/high risk, full demo smoke passes twice from reset.

**Risks:** stochastic model misses. Prompt/input tuning is allowed; weakening validators or hard-coding output is not. If quality remains below target, mark architecture/build not ready.

**Do not implement:** new features to mask failures, fixture-specific report IDs, precomputed outcomes.

## Phase 13 — Demo and submission hardening

**Goal:** repeatable five-minute presentation and honest submission.

**Dependencies:** Phase 12 pass.

**Files/modules:** `docs/plans/demo-plan.md`, operator checklist/runbook, screenshots/video outside core runtime, final README/status.

**Tasks:** rehearse timing/persona transitions; warm runtimes without precomputing outputs; verify SES address/quota/token/clock; test reset and cleanup; failure narration; record backup video; cost cleanup; freeze dependency/prompt/policy/model/runtime versions; update project status only to what works.

**Tests:** three consecutive reset-to-missed flows; one injected Bedrock failure and one SEND_UNKNOWN drill; laptop/network/browser rehearsal; final secret/private-data diff scan.

**Exit criteria:** live path fits five minutes with a one-minute buffer, backup materials exist, cleanup/rollback known, docs match deployed code, no fake claim.

**Risks:** live AWS/network/model variability. Warm/smoke beforehand, retain visible typed failures, use recorded backup only as presentation contingency.

**Do not implement:** post-freeze refactors, new adapters/scenarios, cosmetic work that risks security paths.
