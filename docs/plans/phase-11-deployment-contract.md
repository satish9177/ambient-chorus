# Phase 11 deployment contract

The frozen deployment contract for Phase 11 — AWS deployment and AgentCore hardening. It adds no
product decision. Every semantic here is already frozen in [ADR-010](../adr/ADR-010-agentcore-runtime.md),
[ADR-018](../adr/ADR-018-safe-evidence-and-compile-commit.md) through
[ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md), and
[02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md);
what this document freezes is *how the already-decided system is deployed*, in what order, under
which identity, and what must be proved live before Phase 11 is accepted.

**ADR-029 is Accepted; ADR-030 remains Proposed.**
[ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md) freezes the deployed demo clock.
[ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) (the live SES receipt path), and the
dependent clauses in § 10.2, § 10.3 and canaries K–K3, still require acceptance before
implementation. This documentation repair does not authorize Phase 11 implementation.

**Status: NOT READY TO DEPLOY.** § 21 classifies every outstanding item. Four are user
prerequisites; the rest are implementation work, most of which can begin offline today.

## 1. What Phase 11 owns, exactly

Phase 11 is the **static-now / live-in-Phase-11** split cashed out. Phases 5–10 built every
artifact, its role, its policy, its log group, and its static template assertion, and deployed
none of them.

**Identity / data / log / queue resources** (Phases 5–10): 7 stacks, ~39 resources — 3 tables,
2 evidence buckets, 2 KMS keys, 8 IAM roles, 6 log groups, the schedule group, the DLQ + its
key, the DLQ alarm, and the SES configuration set. Zero AgentCore runtime, zero SES identity,
zero web.

**Phase 11 batch 5 (offline) adds compute and the ingress**, and after the review repair the
synthesized offline app was **7 stacks, ~55 resources** (Macro A takes it to 10 — see the
Macro A note below). The five production Lambdas synthesize
under their **pre-existing** execution roles and dedicated log groups — `chorus-api-{env}`,
`chorus-worker-{env}` and the HTTP API in `AmbientChorusApplication`; `chorus-compiler-{env}`,
`chorus-sender-{env}`, and `chorus-commitment-watcher-{env}` in their own stacks — Python 3.12,
x86-64 (no Lambda architecture is frozen; x86-64 is the least-risky choice for the existing
dependency closure, and AgentCore's ARM64 decision is untouched), each with a physical name
taken directly from the configured environment string. Every deployment stack is created for the
frozen region `us-east-1` (`Environment(region=…)`; `CdkBuildConfig` rejects any other region).
The watcher gains a published `Version` and an `Alias` named exactly `live`; the API Gateway v2
**HTTP API** gains one `$default` proxy integration at **payload format version 2.0** and its
`$default` stage with auto-deploy. The compiler / sender / watcher-alias are named by their
**actual resource ARNs** (cross-stack `Fn::ImportValue`), not independently constructed
literals; the demo-access / cursor-signing / destination-registry **secret identities** (ARNs
only — no value synthesized) and the scheduler identity are wired by `infra/cdk/app.py`.

Only the **worker** carries the three AgentCore runtime-endpoint ARNs — it is the only function
that invokes an agent. The API, compiler, sender, and watcher carry `agent_mode=agentcore` (a
demo-wide invariant) and nothing else AgentCore: the "and all six runtime/profile ARNs" clause
moved out of the global `Settings` validator into the worker's own settings mapper. No batch-5
component consumes a model-profile ARN.

**Two synthesis modes, chosen explicitly.** Deployment-capable mode (the default for
`python infra/cdk/app.py` / `build_app()`) refuses a missing Lambda ZIP and a synthetic /
malformed / account-zero / wrong-service / wrong-region deployment identity — **including an
ARN whose *resource portion* is the wrong shape**, a Secrets Manager ARN that does not name a
`secret:` resource or an AgentCore ARN that stops at `runtime/<id>` with no
`/runtime-endpoint/<id>` — **before synth**, with no silent placeholder fallback. Offline-review
mode is `build_app(offline=True)`, `-c offline_synth=true`, or `CHORUS_CDK_OFFLINE_SYNTH=1`, and
uses the clearly-named `_lambda_placeholder` code and synthetic identity fixtures; its asset
identity differs from any real ZIP's. CI runs `npm run cdk:synth:offline` (a dedicated script
that sets `-c offline_synth=true`); the plain `npm run cdk:synth` remains deployment-capable and
fails closed.

**Deployment artifacts** are built by `tools/build_lambda_artifacts.py` from `uv.lock`
(`--no-default-groups`, `--python-platform x86_64-manylinux_2_28`, `--only-binary :all:`) and
gated: the repository's own credential patterns over the first-party files (zero exceptions) and
over the final ZIP, an ELF `e_machine` check over every shared object including versioned
libraries (`libjpeg-*.so.62`), and a narrow per-match suppression for exactly one pinned
`PIL/ImageFont.py` false positive. A clean-checkout release gate (session fixture; run for real
on the CI Linux/Python-3.12 runner) builds all five ZIPs into a temp directory and scans the
finished ZIPs on every platform. On **Linux/x86-64** it additionally unpacks each ZIP and runs
a fresh `python -S -E` whose `sys.path` is rebuilt to **only** the extracted archive plus the
interpreter's own standard-library directories (discovered via `sysconfig` — `stdlib`,
`platstdlib`, `lib-dynload`): the handler, `chorus`, `pydantic_core` and its compiled
`_pydantic_core`, and `PIL` for the compiler then resolve from the **archive** while
`importlib` / `json` / `pathlib` resolve from **Python itself**, with the repository checkout,
`.venv`, host site-packages, user site, `PYTHONPATH`, and all network access excluded. On any
other platform (the developer's Windows machine) that last step is a documented `sys.platform`
skip; this repair was **verified for structure, ELF, secret scan, and determinism on Windows
and has not been executed on the Windows machine's Linux probe.**

**Phase 11 Macro A (offline) adds the network foundation, the VPC Lambda attachment, the reset
authority, and base observability.** The synthesized offline app is now **10 stacks**:
`AmbientChorusNetwork` (one VPC, two isolated subnets in two configured AZs, no NAT/IGW/EIP,
six interface + two gateway endpoints, endpoint and workload security groups); the worker,
compiler, and sender `Function`s gain a `VpcConfig` on those two subnets and exact inline ENI
IAM (no `AWSLambdaVPCAccessExecutionRole`) — the API and watcher deliberately stay out;
`AmbientChorusReset` (a dedicated `chorus-demo-reset-{env}` role and function, VPC-attached, no
public route, every grant bounded to the delimiter-aware `["NS#DEMO", "NS#DEMO#*"]` grammar and
`ns/DEMO/*`) with the **full deployed reset semantics** — `chorus.composition.deployed_demo_reset`
runs the frozen manifest-driven sequence (persisted `DemoManifest`, `DEMO_RESET_LOCK`, durable
idempotent replay, bounded no-scan partition/prefix/schedule purge, the ADR-029 § 3
generation-fenced clock reseed, post-purge verification) against production adapters and calls
the **shared** `DemoResetService` seed/receipt; and `AmbientChorusObservability` (a Lambda error
alarm per deployed function and one `chorus-{env}` dashboard). Review R1–R5 repaired the ENI IAM
(`SourceFunctionArn` on the code-blocking DENY, not the service-side ALLOW), the exact DEMO
namespace grammar, the bounded AgentCore S3 endpoint exception, and the reset S3/KMS access.

**Still absent:** the three AgentCore Runtime resources and their inference profiles, the
artifact **upload**, the inbound-mail Lambda and the ADR-030 SES receipt path, AgentCore
runtime alarms, the `DemoManifestRegistrar` call-site wiring into the dynamic-entity creation
paths (a named implementation seam — the port and adapter exist), live canaries, and any live
deployment. **The compute resources exist only in an offline synthesis; deployment remains
blocked** by the AgentCore/inbound stacks and the user prerequisites in § 21.

**What already exists in code and must be credited rather than rebuilt.** The AWS adapters are
written, typed, and tested: `DynamoDbStorageDriver` and the three repositories, `S3ObjectStore`,
`SesV2EmailSender` with its outcome classifier, `EventBridgeDeadlineScheduler` (frozen
`FlexibleTimeWindow=OFF`, `ActionAfterCompletion=DELETE`, `ClientToken`, max age 3600 s, max
retries 3, DLQ target, and classified-never-raised outcomes), `Boto3AgentCoreInvoker`,
`CompilerSendAuthorization`, and the full inbound attester/verifier. Phase 11 does not write
adapters. It writes the six things that bind them to AWS:

1. ~~**the AgentCore HTTP server binding**~~ — **done** (§ 5). All three `runtime.toml` now
   declare `server_binding = "IMPLEMENTED"`, bound to a bare ASGI application on the already-locked
   `uvicorn`; `bedrock-agentcore` is still not a dependency and the binding does not import it;
2. **Lambda handlers** — `functions/*` contains composition roots and **no `handler`**; there is
   no ASGI adapter (`mangum` is absent) and no Secrets Manager client anywhere in the repository;
3. **a deployed composition root** — `build_local` constructs `InMemoryStorageDriver` itself and
   refuses any driver that is not `NamespaceStorePurge`;
4. **the `InboundMailTransportAuthenticator`** ([ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md) § 1);
5. **the `SesEventTransportAuthenticator`** ([ADR-025](../adr/ADR-025-one-deliberate-ses-attempt.md) § 10);
6. **the artifact build/publish pipeline and the deploy/canary CLI.**

**Documentation correction.** [11-frontend-and-demo.md](../architecture/11-frontend-and-demo.md)
says "Phase 11 deploys this composition against real adapters; it does not write a second one."
`build_local` cannot do that. Phase 11 either parameterises it by adapter set or adds
`chorus.composition.aws`; the sentence must be corrected in the same change. No wiring, port, or
invariant changes either way.

## 2. Deployment identity

**Deployment MUST NOT proceed as the account root.** Not because the account was once used as
root — that is history, not a blocker — but because using root *now* would make every IAM canary
in § 15 meaningless: a denial proved under an identity that can grant itself the permission
proves nothing.

Two identities, and they are not the same:

**A. Bootstrap identity.** `cdk bootstrap` creates the `CDKToolkit` stack, the staging bucket,
the ECR repository, and the four `cdk-hnb659fds-*` roles. Creating IAM roles that can administer
an account genuinely requires broad authority, so this identity is broad — and it is used **once**,
by a human, non-root, and then not again. Freeze the bootstrap's own output rather than the
human's permissions: `--cloudformation-execution-policies` is pinned to a customer-managed
`ChorusDeployExecutionPolicy` rather than left at the default `AdministratorAccess`, and the
trust policy on `cdk-hnb659fds-deploy-role` is scoped with `--trust` to the deployer principal.
The bootstrap roles are the real deployment authority; bounding them is what bounds deployment.

**B. Normal deployer.** An IAM Identity Center permission set, `ChorusDeployer`, assumed by
`aws sso login`. Short-lived credentials only. Its own policy is small: `sts:AssumeRole` on
`arn:aws:iam::{account}:role/cdk-hnb659fds-*-{account}-us-east-1`, plus `cloudformation:*` on
`arn:aws:cloudformation:us-east-1:{account}:stack/AmbientChorus*/*` and the `CDKToolkit` stack.

**A naming restriction on the human does not restrict what the assumed role can do.** Once
`ChorusDeployer` assumes `cdk-hnb659fds-deploy-role`, that role's `cloudformation-execution`
policy is the only thing bounding the deployment. That is why the execution policy is pinned in
bootstrap and why it, not the permission set, is the artifact to review.

**No access keys.** None in `.env`, none in CI, none in the repository. `.env` is not deployed
secret storage (§ 13).

**Verification, before every deploy:**

```bash
aws sts get-caller-identity --query 'Arn' --output text --region us-east-1
```

**Refusal condition — normative.** If the ARN matches `arn:aws:iam::\d+:root`, or `UserId`
equals `Account`, **stop**: no deploy, no bootstrap, no resource. The deploy CLI implements this
and exits non-zero; a shell habit is not a control. The same check refuses an account other than
the frozen one.

Current state: the local CLI session is expired, so no identity can be verified right now (P1).

## 3. Region and account

**Region: `us-east-1`**, retained. The reasons are alignment and sufficiency, not exclusivity:

- it is what `Settings.aws_region`, `02` § AWS resource layout, and every checked-in default
  already say, and changing it now would touch configuration, ARNs, and docs for no gain;
- it supports AgentCore Runtime, SES email receiving, and Nova 2 Lite inference;
- it is a valid source region for the `us.` geographic cross-region inference profile (§ 4).

It is **not** claimed to be the only region where those coincide. No comparison was run and none
is needed — there is no demonstrated benefit to moving.

**Region is pinned explicitly everywhere**: `--region us-east-1` on every CLI call,
`CHORUS_AWS_REGION=us-east-1` in every deployed environment, `env=Environment(region="us-east-1")`
on every stack. The local CLI default of `ap-south-1` is **configuration drift, not an external
blocker** — it is resolved by never inheriting the default. The deploy CLI's identity check also
asserts the resolved region.

**Account:** to be supplied. Recorded in deploy config, never in a source file.

## 4. Model and inference-profile contract

**Base model, unchanged and not substituted: `amazon.nova-2-lite-v1:0`**, temperature `0`.

**Deployed target: `us.amazon.nova-2-lite-v1:0`** — the US geographic cross-region system
inference profile. Each agent gets its **own application inference profile** derived from that
system profile, preserving the frozen one-profile-per-agent design for IAM scoping and cost
attribution. Three profiles, three agents, unchanged.

**There is no Marketplace subscription for Amazon Nova.** Any implication otherwise is removed.
What is required is ordinary Bedrock model access for the account and region, which is verified,
not assumed:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'nova-2-lite')]"
```

**Runtime IAM.** Each agent role allows `bedrock:InvokeModel` — and
`bedrock:InvokeModelWithResponseStream` only if the adapter streams; today it does not, so the
streaming action is granted only when the adapter is changed to use it — on **two** resource
sets:

- **A.** that agent's exact application inference profile ARN, taken from a deployment output.
  **Never constructed from a name**: an application inference profile ARN ends in a
  service-generated identifier, and a synthesized ARN built from `chorus-monitor-demo` names
  nothing. The existing `_default_profile_arn` helper is a placeholder and must be replaced by
  discovery.
- **B.** the foundation-model ARNs the US geographic profile routes to, **condition-bound to that
  exact inference profile** so the grant is usable only through the profile it belongs to:

```jsonc
{
  "Sid": "InvokeMonitorInferenceProfileOnly",
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": [
    "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0",
    "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-2-lite-v1:0",
    "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-2-lite-v1:0"
  ],
  "Condition": { "StringEquals": { "bedrock:InferenceProfileArn": "<discovered monitor profile ARN>" } }
}
```

Currently verified destination regions for the US profile: **`us-east-1`, `us-east-2`,
`us-west-2`.** The set is re-read at deploy time from the system profile's own `models` list, not
hard-coded from this document, and the deploy CLI fails if it has grown a region the policy does
not name.

**SCP and account restrictions must be documented for every destination region.** A cross-region
profile routes inference to regions the request did not name. If a service control policy, a
region opt-in setting, or a data-residency requirement restricts `us-east-2` or `us-west-2`, the
invocation fails there in a way that looks like a model error. Confirm all three regions are
enabled and unrestricted for `bedrock:InvokeModel`, and record the answer.

**A live invocation canary under every deployed agent role is required regardless** (canary J).
Policy analysis does not prove a cross-region profile invocation works.

**Do not substitute Nova Lite.** If `us.amazon.nova-2-lite-v1:0` cannot be invoked, report the
concrete failure; a model change needs evaluation evidence and an ADR, per
[00-system-overview.md](../architecture/00-system-overview.md) § 114.

## 5. AgentCore runtime contract

Verified against the installed **`aws-cdk-lib 2.267.0`**:

- **The construct is `aws_bedrockagentcore.Runtime`.** `AgentCoreRuntime` is *not* the construct —
  it is the runtime-environment class supplying `PYTHON_3_10 … PYTHON_3_14 | NODE_22`. The earlier
  draft named it as the construct and was wrong.
- Artifact: `AgentRuntimeArtifact.from_s3(s3.Location(bucket_name=…, object_key=…),
  AgentCoreRuntime.PYTHON_3_12, ["python", "main.py"])`. Direct-code Python 3.12 from an S3 zip;
  **no ECR image required**. The command is the frozen minimum start command below, not the OTEL
  wrapper this line used to show.
- Authorizer: `RuntimeAuthorizerConfiguration.using_iam()` (Python snake_case for `usingIAM`);
  it is also the default.
- Network: `RuntimeNetworkConfiguration.using_vpc(scope, vpc=…, security_groups=…, vpc_subnets=…)`.
- Endpoints: `runtime.add_endpoint("live", version="N")` → `RuntimeEndpoint`.
- Also available and used: `execution_role`, `environment_variables`, `tracing_enabled`,
  `logging_configs`, `manage_delivery_resource_policy`.

**Runtime names must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — letters, digits, and underscore
only, starting with a letter. Hyphens are invalid.** The manifests declare `name = "chorus-monitor"`,
which cannot be a runtime name. Frozen deployed names:

| | Monitor | Investigator | Action |
|---|---|---|---|
| Runtime name | `chorus_monitor` | `chorus_investigator` | `chorus_action` |
| Execution role | `chorus-monitor-runtime-demo` | `chorus-investigator-runtime-demo` | `chorus-action-runtime-demo` |
| Log group | `/aws/bedrock-agentcore/chorus-monitor-demo` | …`-investigator-demo` | …`-action-demo` |
| Prompt version(s) | `monitor/v3` | `investigator/v1` **and** `commitment-extraction/v1` (§ 11) | `action/v1` |
| Runtime budget | 60 s | 75 s | 60 s |
| Model read timeout | 45 s | 60 s | 45 s |

IAM role names keep their hyphens — the constraint is on the runtime resource only. Each
`runtime.toml` gains a `deployed_name` field so the mapping is declared, not inferred.

**Server binding.** AgentCore invokes the runtime over HTTP and requires two routes:

| Route | Method | Contract |
|---|---|---|
| `/ping` | `GET` | health; returns `200` with a status body, touches no model |
| `/invocations` | `POST` | receives the raw request body, returns the raw response body |

`main.py` binds `/invocations` to the existing `handle(raw: bytes) -> bytes` and nothing else.
It is a transport adapter: no branching on payload content, no state between calls, no second
place where a contract is enforced. `handle` remains the only entry point, so every existing
boundary test still covers the deployed path.

**The binding is implemented.** All three `runtime.toml` now declare `server_binding =
"IMPLEMENTED"`, backed by a test that imports the entrypoint they name and asserts it is bound to
that runtime's own handler. It is a bare ASGI application (`runtimes/server.py`) served by
`uvicorn`, chosen because `uvicorn` is **already inside the artifact's locked dependency
closure** — `strands-agents → mcp → sse-starlette → uvicorn` — so two routes cost fifty lines and
no wheel. `bedrock-agentcore` is not a dependency and the binding does not import it.

**Entrypoint command: `["python", "main.py"]`.** Corrected from
`["opentelemetry-instrument", "main.py"]`. `uv pip install --target` resolves *wheels* for the
declared platform but generates console-script launchers for the **build host**, so a Windows
build writes `bin/opentelemetry-instrument.exe` — unusable on Linux/ARM64, and `bin/` is not on
the runtime's path in any case. The build strips those launchers and the artifact inspection
refuses them. `CHORUS_OTEL_ENABLED` stays `false` for the minimum launch (§ 19 already omits the
X-Ray endpoint by default), and the OTEL auto-instrumentation wrapper is revisited only if live
wiring proves a Linux-compatible form of the command. `opentelemetry-instrumentation` still ships
*as a package* inside the artifact, by way of `strands-agents`.

**Import layout.** The zip is rooted so `runtimes.<agent>` and `chorus.contracts` resolve exactly
as they do in the repository — the manifest's `[artifact].include` allowlist is a set of
repo-relative paths and is preserved verbatim in the archive. `main.py` sits at the archive root.

That layout has one consequence the allowlist does not state, and it is fatal if missed: the
shared package lands at `src/chorus/...` while `python main.py` puts only the archive **root** on
`sys.path`, so a runtime that imported nothing else would fail at cold start with
`ModuleNotFoundError: No module named 'chorus'`. The repository never sees it, because the
editable install already exposes `src`. `main.py` therefore calls
`runtimes.bootstrap.ensure_shared_package_importable` **before** any other import: it puts the
archive's own `src` at the front of `sys.path` and refuses to start if `chorus` is still not
importable. The allowlists carry `src/chorus/__init__.py` (and `src/chorus/domain/__init__.py`
where the domain ships) so the archive's package structure is the repository's rather than a
namespace-package approximation of it. An isolated-startup test extracts each real zip outside
the repository, with no site-packages and no `PYTHONPATH`, and proves `chorus` resolves out of
the archive and from nowhere else.

**Artifact secret gate.** `tools/check_secrets.py` ignores `build/`, because the artifact build
unpacks third-party wheels whose *documentation* is credential-shaped (`boto3`'s CloudFront
example, `botocore`'s IAM and STS example documents, `cryptography`'s SSH parser). That exemption
is correct for the repository scan and must never be what makes an artifact publishable, so the
build carries its own gate, applying the repository's own patterns twice: once over the
first-party files it just staged, and once over the finished zip. **First-party entries get no
content exception at any path.** Vendored entries are checked for credential filenames and
private-key material with exactly the four documented path exceptions above; private keys are
detected by content rather than by extension, so `certifi/cacert.pem` — public CA certificates
every TLS call depends on — is not refused. The build manifest records `security_scan`, which a
publish step reads to know a scan ran and never instead of re-scanning the zip.

**Packaging must be Linux-compatible.** The build runs on Windows. `pydantic-core` ships compiled
wheels, so dependencies are resolved with an explicit target rather than the host platform:
`--python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary :all:`. A build that
silently vendored Windows wheels produces a runtime that fails at import with no useful message.

**Environment variables** per runtime: its model profile ARN variable, `CHORUS_AWS_REGION`, and
`AWS_REGION` — the entrypoint reads `CHORUS_AWS_REGION` but botocore reads `AWS_REGION`, and both
must be set to `us-east-1`. Nothing else; no secret, no table name, no bucket name.

**Version, endpoint, and rollback.** Each `Runtime` update creates an immutable version. One
`RuntimeEndpoint` named `live` points at a chosen version; the application invokes through the
endpoint and passes it as the invoker's `qualifier`. **Rollback is repointing `live` at the
previous version** — no rebuild, no republish, no data touched. CDK outputs the runtime ARN, the
endpoint ARN, and the version; the application receives them as environment variables and never
looks them up at runtime.

**Logging.** `logging_configs` sends `APPLICATION_LOGS` to the agent's own pre-created log group.
`manage_delivery_resource_policy` is left on for the first runtime and set `false` on the other
two — three runtimes would otherwise consume three of the account's ten CloudWatch Logs
resource-policy slots.

Three runtimes, three roles, three log groups, three profiles. No Graph, no swarm, no workflow,
no agent-to-agent invocation — already an IAM fact via the `bedrock-agentcore:InvokeAgentRuntime`
deny on all three roles.

## 6. AgentCore artifact loading, and the deny that currently blocks it

**A defect in the synthesized agent policy.** Each runtime role is granted `s3:GetObject` on its
own artifact prefix and, in the same policy, denied `s3:GetObject` on `Resource: "*"` as part of
`DENIED_DATA_PLANE_ACTIONS`. **An explicit deny always wins.** As synthesized, every agent runtime
is denied its own artifact and cannot cold-start.

**The repair, which weakens no isolation.** Split the one blanket S3 deny into two:

```text
DenyEvidenceObjects   DENY  s3:GetObject, s3:PutObject, s3:DeleteObject, s3:ListBucket
                            on the private and export bucket ARNs and their objects
DenyObjectMutation    DENY  s3:PutObject, s3:DeleteObject, s3:ListBucket
                            on "*"
```

Private-evidence and export isolation is now stated against the buckets it is about, which is
*stronger* than a wildcard that had to be walked back. No agent can read either evidence bucket,
write any object anywhere, or list any bucket; each can read exactly its own artifact prefix. The
DynamoDB denies are untouched.

**Both artifact sources must be reachable from the isolated subnets:**

- the **customer artifact bucket**, `chorus-agent-artifacts-demo` — a third bucket, separate from
  both evidence buckets and their keys, because agent code is not evidence;
- the **AWS service-owned code bucket** AgentCore reads during cold start. Its ARN is not ours to
  enumerate, so the **S3 gateway endpoint policy must not be a closed allowlist of our own
  buckets**. It admits (a) `s3:GetObject` on the artifact bucket's agent prefixes, (b)
  `s3:GetObject`/`s3:PutObject` on the two evidence buckets, and (c) `s3:GetObject` on the
  service-owned AgentCore code path. A gateway endpoint policy that named only (a) and (b) would
  produce a cold-start failure that reads as a runtime error, and this is the one place where an
  over-tight endpoint policy is silently fatal. Endpoint policy is defence in depth; the role
  policies remain the authority.

**Security-group egress** for the runtime SGs permits TCP 443 to the S3 managed prefix list
(`com.amazonaws.us-east-1.s3`) in addition to the interface-endpoint SGs. A rule listing only the
endpoint SGs blocks gateway-endpoint traffic, which does not flow through them.

**Availability zones.** AgentCore supports a specific set of AZs, identified by **AZ ID**
(`use1-az1`-style), and AZ IDs map to different AZ *names* in different accounts. The two isolated
subnets are placed by resolving the supported AZ IDs into this account's names
(`aws ec2 describe-availability-zones --query 'AvailabilityZones[].[ZoneName,ZoneId]'`) and pinning
the resulting names in deploy config. Choosing `us-east-1a`/`us-east-1b` by name and hoping is how
this fails in a fresh account.

## 7. Network design and the corrected endpoint matrix

[ADR-010](../adr/ADR-010-agentcore-runtime.md) freezes AgentCore VPC mode in two isolated subnets
with no NAT and no internet route, and all three manifests declare `network_mode = "VPC"`. **That
is the reason there is no NAT gateway** — the egress boundary is a frozen security decision.
Cost is a consequence, not the justification, and the earlier draft had that backwards.

**Which components actually need VPC placement.** Not all of them, and the default is *out*:

| Component | VPC? | Why |
|---|---|---|
| Three agent runtimes | **Yes** | frozen by ADR-010 |
| Compiler Lambda | **Yes** | holds private-bucket and private-key access; the one component whose network reachability should match its data reach |
| Sender Lambda | **Yes** | holds the destination secret and the only SES send grant |
| Inbound entry point | **Yes** | writes and reads private raw MIME |
| Worker Lambda | **Yes** | invokes the runtimes, which are VPC-only |
| API Lambda | **No** | it is a request-path front end behind API Gateway; its data access is the same table and bucket APIs, reachable from the Lambda service network. Placing it in the VPC buys no boundary and adds ENI cold-start latency to the one component a presenter waits on |
| Watcher Lambda | **No** | DynamoDB only, no private data, no secret |

Keeping two Lambdas out of the VPC is a deliberate reduction, not an oversight: neither touches
private evidence, and both benefit from faster cold starts on the demo path.

**Endpoints — derived from the six boto3 clients this system actually constructs**
(`dynamodb`, `s3`, `lambda`, `scheduler`, `sesv2`, `bedrock-agentcore`) plus the runtimes' model
calls:

| Endpoint | Type | Required by | Notes |
|---|---|---|---|
| `com.amazonaws.us-east-1.bedrock-runtime` | Interface | agent runtimes | **required** |
| `com.amazonaws.us-east-1.s3` | Gateway | runtimes (artifact), compiler, sender-adjacent, inbound | **required**; free |
| `com.amazonaws.us-east-1.dynamodb` | Gateway | compiler, sender, worker, inbound | free |
| `com.amazonaws.us-east-1.bedrock-agentcore` | Interface | worker → `invoke_agent_runtime` | data plane only |
| `com.amazonaws.us-east-1.lambda` | Interface | worker → compiler; sender → compiler fence | |
| `com.amazonaws.us-east-1.secretsmanager` | Interface | sender (destination registry) | |
| `com.amazonaws.us-east-1.scheduler` | Interface | worker → `CreateSchedule` | |
| `com.amazonaws.us-east-1.email` | Interface | sender → SESv2 `SendEmail` | **the SESv2 API endpoint is `email`** — not `sesv2`, not `email-smtp`. `sesv2` is the boto3 client name; SMTP is a different protocol this system does not use |
| `com.amazonaws.us-east-1.logs` | Interface | **only** if a component makes direct CloudWatch Logs API calls | Lambda and AgentCore log delivery is service-managed and does not traverse the VPC. Omit unless OTLP export needs it |
| `com.amazonaws.us-east-1.xray` | Interface | **only** if `CHORUS_OTEL_ENABLED=true` | omit by default |

**Explicitly NOT required**, and not created merely because the service exists:

- **KMS** — no `kms` client is constructed anywhere. SSE-KMS is performed by S3 server-side; the
  caller never speaks to KMS. Key *policies* still matter; a network endpoint does not.
- **STS** — Lambda and AgentCore credentials arrive through the execution environment.
- **`bedrock-agentcore-control`** — the control plane is a deployment-time concern, reached by
  CloudFormation, not by any runtime component.
- **`execute-api`** — nothing inside the VPC calls the API.
- **SNS / SQS** — no component constructs those clients. If the SES event destination or the
  inbound notification path is implemented over SNS or SQS, the corresponding endpoint is added
  *then*, with the code that needs it.

**Required interface endpoints: 6. Gateway endpoints: 2 (free). Conditional: 2.** The earlier
draft listed nine interface endpoints including KMS, STS-adjacent, and logs; that inventory was
wrong.

**No NAT** unless a real service dependency proves it impossible. None has.

## 8. IAM — corrections

Eight roles exist and synthesize. Phase 11 makes **five** corrections, each a repair of something
already frozen in `02`, not a new decision.

### 8.1 Split the API and worker principals

`ChorusApplicationStack` synthesizes **one** role described as "FastAPI application and operation
worker". `02` separates them. A single role is the union of two, which makes the demo-token deny
unassertable and hands the request-path role the agent-invoke grant.

| | **API Lambda role** `chorus-api-demo` | **Worker Lambda role** `chorus-worker-demo` |
|---|---|---|
| READ | Core (all), Shareable (all safe), private S3 + private KMS, export S3 (decrypt), **demo-token secret**, strong read of `NS#DEMO#CLOCK` | same, **minus the demo-token secret**; strong read of `NS#DEMO#CLOCK` (§ 8.9) |
| WRITE | Core; Shareable `ACTION#`/`ACTION_CURRENT#`/`EXECUTION#`/`CASE#`; Audit; the forward-only guarded CAS on `NS#DEMO#CLOCK`; `ConditionCheck` on `VIEW_CURRENT#` | same, minus the clock CAS — and **denied** every clock write by name |
| INVOKE | worker Lambda, compiler Lambda, **commitment watcher `:live` alias** (§ 8.9) | **Monitor / Investigator / Action runtimes**, compiler, sender |
| SCHEDULER | — | `CreateSchedule` + `GetSchedule` on `schedule/chorus-demo/*`; `iam:PassRole` on the scheduler execution role alone, `iam:PassedToService = scheduler.amazonaws.com` |
| DENIED | SES, Bedrock, `bedrock-agentcore`, view-prefix writes, destination secret, `DeleteSchedule`/`UpdateSchedule` | SES, direct Bedrock, **demo-token secret**, destination secret, view-prefix writes, `DeleteSchedule`/`UpdateSchedule` |

**No agent runtime invocation on the API role.** No route invokes a runtime directly; every
agent-invoking operation is dispatched to the worker and returns 202. If a route is ever found
that needs it, that is a design change, not a grant to add quietly.

Both roles are missing `lambda:InvokeFunction` and `secretsmanager:GetSecretValue` entirely today
— neither is granted anywhere in the application stack. Both are added with exact function and
secret ARNs.

### 8.2 Repair the compiler's Shareable read authority

**The compiler role has no Shareable read grant at all.** It holds `WriteViewPrefixesOnly` and
nothing else on that table. But the send-authorization service — which runs *inside the compiler
Lambda*, invoked by the sender through `CompilerSendAuthorization` — reads:

| Call | Shareable partition |
|---|---|
| `load_view` | `NS#*#VIEW#*` |
| `load_current_view_pointer` | `NS#*#VIEW_CURRENT#*` |
| `load_current_action_pointer` | `NS#*#ACTION_CURRENT#*` |
| `load_proposal` | `NS#*#ACTION#*` |
| `load_approval` | `NS#*#ACTION#*` |

and `CompileView` reads the first two. Every one fails with `AccessDenied` in the deployed system.
The send fence can never be acquired, so **no send can ever happen** and the demo's 3:00–3:45
segment is impossible.

`02` § IAM already says `Compiler Lambda | … | R(all safe)/W(view only)`. The document is right;
the CDK is missing the read. **This is a code defect, not a design change, and needs no ADR.**

The repair, narrower than the document requires and sufficient for every call above:

```text
ReadShareableViewAndActionPrefixes  ALLOW  GetItem, BatchGetItem, Query
  on the Shareable table
  ForAllValues:StringLike dynamodb:LeadingKeys =
    NS#*#VIEW#*, NS#*#VIEW_CURRENT#*, NS#*#ACTION#*, NS#*#ACTION_CURRENT#*
```

`EXECUTION#` is deliberately excluded — the compiler never loads an execution; the sender loads
its own. `CASE#` on the Shareable table is excluded — those partitions are the watcher's.
**No compiler write is widened.** The compiler still cannot call Bedrock or SES, still writes only
`FENCE#` in Core and the two view prefixes in Shareable, and still holds only `ConditionCheckItem`
on case partitions.

### 8.3 Add the foundation-model resources to the three agent statements

Per § 4: the profile ARN alone is not sufficient to invoke through it.

### 8.4 Fix the artifact deny

Per § 6.

### 8.5 Grant the scheduler execution role something

`chorus-scheduler-demo` is created with a trust policy and **zero attached policies**. It needs
`lambda:InvokeFunction` on the watcher function ARN, `sqs:SendMessage` on the DLQ, and
`kms:GenerateDataKey` + `kms:Decrypt` on the DLQ key. Its trust policy is additionally scoped with
`aws:SourceAccount` and `aws:SourceArn` on the schedule-group ARN.

### 8.6 Lambda VPC execution permissions — an honest wildcard

Every VPC-attached Lambda role needs the six ENI operations:
`ec2:CreateNetworkInterface`, `DescribeNetworkInterfaces`, `DeleteNetworkInterface`,
`AssignPrivateIpAddresses`, `UnassignPrivateIpAddresses`, `DescribeSubnets`.

**These require `Resource: "*"`.** `CreateNetworkInterface` acts on a resource that does not exist
yet, and the `Describe*` calls are not resource-scoped at all. This is an AWS-imposed wildcard, and
the earlier draft's claim that X-Ray was the only legitimate allow wildcard was wrong.

It is constrained rather than merely admitted:

```jsonc
"Condition": {
  "ArnEquals":   { "lambda:SourceFunctionArn": "<this function's ARN>" },
  "StringEquals":{ "ec2:Subnet": ["<subnet-a>", "<subnet-b>"] }
}
```

`lambda:SourceFunctionArn` binds the permission to calls the Lambda service makes on this
function's behalf, so function code that reached for `ec2:CreateNetworkInterface` itself would be
denied. Where `ec2:Subnet` is not supported for a given action, `lambda:SourceFunctionArn` alone
carries it. `DescribeSubnets` and `DescribeNetworkInterfaces` remain unconditioned reads of
non-sensitive network metadata; that is stated rather than dressed up.

### 8.9 Two contradictions, resolved exactly (Phase 11 batch 4)

Building the production handlers surfaced two places where the frozen composition and the frozen
policy disagreed. Both are resolved here, narrowly, and each resolution is asserted from the
synthesized template rather than argued from this document.

**A. The API invokes the commitment watcher, synchronously.**

`POST /v1/demo/clock/advance` returns `{logical_now, watcher_outcome, commitment_status}`. Its
frozen response therefore promises *what the watcher decided*, not that a decision was
scheduled — and § 8.1 gave the request path no watcher invoke authority at all. Routing the
endpoint through the asynchronous worker would resolve the permission by changing the promise,
so it is not done. The API instead:

1. advances the durable clock through the normal clock port (§ 8.9 B, ADR-029 § 3);
2. invokes the watcher **synchronously** and parses its typed answer;
3. returns the accepted response.

```text
InvokeCommitmentWatcherLiveAliasOnly  ALLOW  lambda:InvokeFunction
  Resource = arn:aws:lambda:{region}:{account}:function:chorus-commitment-watcher-{env}:live
```

The **qualified `live` alias** and nothing else: no unqualified function ARN, no numeric
version, no wildcard. Rollback repoints the alias at a published version and this statement does
not change (§ 20). It is a statement of its own rather than a third resource on
`InvokeOperationWorkerAndCompilerOnly`, so widening one cannot silently widen the other.
`CHORUS_WATCHER_FUNCTION_ARN` carries the alias ARN. The worker holds no watcher invoke.

**B. The worker genuinely requires authoritative logical time, read-only.**

[ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md) § 2 listed the worker as holding no
clock authority, while `EXTRACT_COMMITMENT` — which runs on that principal — supplies
`clock.now()` as the `logical_now` of its `CreateDueSchedule` request. The demo mapping there is
`actual_now + max(10 minutes, logical_due - logical_now)`, so wall time in the `logical_now` slot
schedules a thirty-day deadline thirty days out and the demo's deadline segment stops working.
Removing the dependency was rejected: it is not dead composition, and replacing logical time
with wall time is the defect, not the fix. `actual_now` itself is the opposite mistake in the
same formula — it must be genuinely wall-clock, never the logical reading this grant supplies;
see § 8.10 F.

```text
ReadDemoClockItemOnly     ALLOW  dynamodb:GetItem
  on the Shareable table, ForAllValues:StringLike dynamodb:LeadingKeys = NS#DEMO#CLOCK
DenyWorkerDemoClockWrites DENY   PutItem, UpdateItem, DeleteItem, ConditionCheckItem
  on the Shareable table, ForAnyValue:StringLike dynamodb:LeadingKeys = NS#DEMO#CLOCK
```

The table-wide `ReadShareable` statement both principals already hold reached this row; the
positive statement makes the authority **stated and assertable** rather than incidental, and the
deny makes "the worker cannot move logical time" an explicit refusal rather than the absence of
a grant. **This amends ADR-029 § 2's principal table for the worker row and nothing else.**

**C. The API's clock write, and the watcher's clock read.**

Neither existed in the synthesized policy before this batch, and both are required by ADR-029:

```text
AdvanceDemoClockItemOnly  ALLOW  dynamodb:PutItem      (API role)
ReadDemoClockItemOnly     ALLOW  dynamodb:GetItem      (watcher role)
  both on the Shareable table, LeadingKeys = NS#DEMO#CLOCK
```

`NS#DEMO#CLOCK` joins the watcher's `FORBIDDEN_WRITE_PREFIXES`, so its total absence of clock
write authority is backed by an explicit deny (ADR-029 § 2).

`PutItem` alone on the write side. The storage driver has no attribute-level update path by
design, so the guarded forward CAS is a conditional whole-item put whose three fences —
`version`, `reset_generation`, and a strictly-earlier stored `logical_time_micros` — are
condition expressions the table evaluates. That also leaves § 8.8's "no `dynamodb:UpdateItem`
anywhere" sentence untouched, which is why it is not amended here.

**Every clock grant in this system names the exact literal `NS#DEMO#CLOCK`.** There is no
`NS#*#CLOCK*` anywhere, template tests sweep for one on every role that touches the partition,
and a policy containing one fails review.

### 8.10 The compiler's clock authority, and the wall/logical split for scheduling (Phase 11 batch 4 repair)

An independent review found two further places where a clock-domain mismatch had reached
production code. Both are resolved here, but **the amendment itself lives in ADR-029, not in
this document** — a plan cannot override an accepted ADR under
[docs/README.md](../README.md)'s own precedence order, so a prior version of this section that
claimed to "amend ADR-029 § 2's principal table" was itself the defect a later review caught.
[ADR-029 § "Accepted Phase 11 batch 4 amendment"](../adr/ADR-029-deployed-demo-clock-authority.md#accepted-phase-11-batch-4-amendment)
is the authoritative principal table; everything below describes the same two repairs and the
IAM template that implements them, and restates that table only for convenience.

**D. The compiler genuinely requires authoritative logical time, read-only.**

Unresolved architectural question 0 (below) asked what clock the deployed compiler stamps a view
with. `functions/compiler/handler.py` supplied `SystemClock`, so a compiled view's
`generated_at` and `expires_at` were wall-clock instants while the case world runs on the demo
logical clock, seeded at `2030-01-14`. A caller comparing a view's expiry against logical time —
exactly what `ProposeAction`'s freshness check does — was comparing two different clocks; a view
minted "now" in logical 2030 could read as already expired against a wall-clock instant in the
2020s. The compiler now reads the durable logical clock once per invocation, the same shape as
the worker's grant in **B** above:

```text
ReadDemoClockItemOnly       ALLOW  dynamodb:GetItem
  on the Shareable table, ForAllValues:StringLike dynamodb:LeadingKeys = NS#DEMO#CLOCK
DenyCompilerDemoClockWrites DENY   PutItem, UpdateItem, DeleteItem, ConditionCheckItem
  on the Shareable table, ForAnyValue:StringLike dynamodb:LeadingKeys = NS#DEMO#CLOCK
```

The compiler's Core authority is unchanged — the clock lives in Shareable. Its existing
Shareable read (`ReadShareableViewAndActionPrefixes`) is **prefix-scoped** to the view and
action `LeadingKeys` it already had reason to read, not table-wide, and `NS#DEMO#CLOCK` is not
one of those prefixes — which is exactly why this is a genuinely new, separate statement rather
than something the existing grant already covered: a table-wide read would have needed no new
policy statement at all, and one was needed. **The compiler's row of ADR-029's effective
principal table is the amendment section's, not § 2's** — see the ADR link above; the sender
and watcher's clock authority is described in **C** and below, and the API's in **A** and **C**.

The chosen resolution is the first of the two the open question named — a read-only compiler
clock grant, mechanically identical to **B** — and not the alternative (an ADR statement that
view lifetime is wall-clock everywhere). The alternative would have meant deciding it is
acceptable for a compiled view's own timestamps to run on a different clock than the case they
describe, which is the defect, not a design option.

**E. The sender's clock authority needed no new statement.**

The sender's `SEND_ACTION` execution and the send authorization fence it reads both reason about
the same logical timeline the compiler and worker use, so the sender's trust-matrix row now
names logical-clock read authority as well. Unlike the compiler, this costs no new IAM
statement: the sender already holds the unrestricted, table-wide `ReadShareable` grant its send
path needs for the send fence and view partitions, and that grant already reaches
`NS#DEMO#CLOCK`. Only the negative side changes — `NS#DEMO#CLOCK` joins the sender's
`FORBIDDEN_WRITE_PREFIXES`, so the existing deny statement that already blocks writes to the
proposal/approval/view/case prefixes now blocks the clock prefix by the same mechanism, and the
absence of clock write authority is an explicit deny rather than an accident of what nobody
granted.

**F. The wall/logical split for real AWS scheduling arithmetic (P2-2).**

**B** above states the demo mapping as `actual_now + max(10 minutes, logical_due - logical_now)`
without saying what supplies `actual_now`. It was, incorrectly, the same logical clock read that
supplies `logical_now` — so with the browser's logical clock advanced into 2030 (the demo's own
seed year), `CreateDueSchedule` computed a real `CreateSchedule` `at_utc` in 2030 real time, an
EventBridge Scheduler resource that would never fire during any demo. The fix is not a new IAM
grant — `actual_now` needs no durable authority at all, only the ordinary process clock — but a
second, distinct `Clock` field on `CreateDueSchedule` (`wall_clock`, alongside the existing
logical `clock`), so the one arithmetic step that anchors a real AWS resource to real time reads
`SystemClock` while every other computation in the same command — `logical_due - logical_now`,
and everything upstream of it in the case world — continues to read the logical clock
unchanged. The worker's principal-table row therefore reads **both** logical-clock read
authority (the grant in **B**) **and** wall-clock read (no authority at all — `SystemClock`
reads no store and needs none), and the two are never the same reading in a single computation.

**The same table, restated here for convenience — [ADR-029's amendment section](../adr/ADR-029-deployed-demo-clock-authority.md#accepted-phase-11-batch-4-amendment)
is the authoritative version if the two ever appear to disagree:**

| Principal | Logical clock read | Logical clock write | Wall clock |
|---|---|---|---|
| API (presenter path) | strongly consistent | guarded forward CAS (§ 3) | not read for any clock-domain decision |
| Worker | strongly consistent (**B**) | none | `SystemClock`, for `CreateDueSchedule`'s `actual_now` only (**F**) |
| Commitment watcher | strongly consistent | none | never |
| Compiler | strongly consistent (**D**, new) | none | never |
| Sender | strongly consistent, via the existing unrestricted grant (**E**, new) | none | never |
| Demo reset principal | strongly consistent | read/write, sole reset exception (§ 3) | never |
| Three agent runtimes, scheduler execution role | none | none | never |

### 8.7 The complete remaining wildcard inventory

| Wildcard | Where | Justification |
|---|---|---|
| `ec2:*NetworkInterface*` on `*` | five VPC-attached Lambda roles | AWS-imposed; constrained by `lambda:SourceFunctionArn` and `ec2:Subnet` |
| `xray:PutTraceSegments` on `*` | agent runtimes | no resource-level ARN exists for segment submission |
| `Resource: "*"` on every DENY | all roles | a deny is only meaningful when it covers everything |
| `dynamodb:*`, `s3:PutObject/DeleteObject/ListBucket`, `bedrock:*`, `scheduler:*` inside DENY | sender, watcher, agents | an action wildcard inside a deny is the strongest form |
| `schedule/chorus-demo/*` | worker | schedule names are derived per commitment; bounded to one group, with `Delete`/`Update` separately denied |
| `{log-group}:log-stream:*` | every role | stream names are runtime-generated; bounded to that role's own group |
| `{artifact-bucket}/{agent}/*` | agent artifact read | content-addressed keys cannot be enumerated at synth; bounded to one agent's prefix |

### 8.8 Preserved invariants

Compiler holds no `bedrock:*` and no `ses:*`; the compiler's only Core write is `FENCE#`; the
sender holds a total Core deny, no model access, and `PutItem` on `EXECUTION#` only; agent
runtimes reach no data store and cannot invoke each other; the Action runtime never receives a
private projection; the watcher cannot create, read, update, or delete a schedule and re-verifies
every event field against a strongly loaded row; the watcher's Core deny stays **total**
([ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md)); no `dynamodb:UpdateItem` and no
blanket `dynamodb:TransactWriteItems` anywhere.

## 9. S3 and KMS — a write defect that fails every object write

`S3ObjectStore` calls `put_object(..., ServerSideEncryption="aws:kms")` for both the inbound raw
MIME and the export evidence derivative, and **omits `SSEKMSKeyId`**. The bucket policy denies
`s3:PutObject` when `s3:x-amz-server-side-encryption-aws-kms-key-id` does not equal the exact key
ARN — and an **absent** condition key does not equal the value, so `StringNotEquals` evaluates
true and the deny fires.

**Every private and export object write returns `AccessDenied` in the deployed system.** No safe
evidence derivative can be written, so no compile can commit; no raw MIME can be stored, so no
reply can be ingested. Nothing in local testing catches it, because no local path evaluates a
bucket policy.

**The repair is on the writer, never the policy.** `S3ObjectStore` is constructed with the exact
key ARN for each bucket and passes `SSEKMSKeyId=<that ARN>` on every `put_object`. The bucket
policies stay exactly as they are — fail-closed, denying both unencrypted uploads and wrong-key
uploads. Relaxing a policy to make a write succeed would delete the guarantee the policy exists
for.

Key ARNs reach the store as deployment configuration (`CHORUS_PRIVATE_EVIDENCE_KEY_ARN`,
`CHORUS_EXPORT_EVIDENCE_KEY_ARN`), from CDK outputs. **Private and export keys remain separate**,
so a principal holding `s3:GetObject` on the private bucket still reads nothing without the
private key's `kms:Decrypt` — the property two keys exist to provide.

Everything else in the data stack deploys as synthesized: Block Public Access all four ways,
bucket-owner-enforced ownership, `enforce_ssl`, versioning, lifecycle expiry (30 d private /
14 d export). `demo` is not in `DISPOSABLE_ENVIRONMENTS`, so PITR and deletion protection are on
and the removal policy is `RETAIN`. The **frontend receives no S3 access of any kind** — no raw
grant, no presigned URL.

**DynamoDB** deploys as synthesized: three tables, `PK`/`SK` string keys, `PAY_PER_REQUEST`,
AWS-managed encryption, TTL on `expires_at_epoch`, **no GSI, no LSI, no streams, no DAX**. All
transaction participant counts (compile; the five-participant action case projection; the
seven-participant reply ingestion) are well within the 100-item limit. One new item type: the
demo clock, Shareable `NS#DEMO#CLOCK` / `DEMO_CLOCK`, carrying
`{logical_time, version, reset_generation, seed_instant, advance_count}`
([ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md)). Every clock grant names that
**exact literal partition** — no `NS#*#CLOCK*` wildcard exists in any role. Clock reads are
strongly consistent everywhere; a missing, corrupt, or unavailable row **fails closed**, and no
component ever falls back to a process-local clock, to `SystemClock` in `demo`, or to the
scheduler event's own timestamp.

## 10. SES — outbound and inbound

### 10.1 Outbound

- Region `us-east-1`; configuration set `chorus-demo` (already synthesized).
- **Verified identities:** a domain identity with DKIM for the sending domain, plus the
  correspondent ("property manager") address or domain as its own verified identity so it can
  send the reply of § 10.2. Custom MAIL FROM is **optional** and not required by anything here —
  [ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) § 5 moved the correspondent comparison to
  the parsed `From` mailbox, whose *domain* DKIM alignment already covers. DMARC PASS is a
  domain-level claim and does not by itself prove control of that mailbox; the mailbox digest
  comparison is what narrows it, and the demo's residual assumption is that the configured
  correspondent mailbox is operated by the intended manager.
- `from_identity_id` stays the opaque `chorus-demo-sender`; only the sender resolves an address,
  from its own secret.

**SES production access is NOT a Phase 11 prerequisite.** Corrected. In the sandbox, both sender
and recipient must be verified identities — and in this demo both are ours. The sandbox caps
apply (200 messages/24 h, 1 msg/s), and the demo sends one. Production access is **optional and
desirable** for flexibility (sending to an unverified judge address, higher rate) and is worth
requesting early, but the hero flow does not depend on it and it does not gate acceptance.

**Phase 8 semantics are unchanged by deployment.** At most one deliberate attempt per approval;
`DRAFT → APPROVED → SENDING`; ambiguity classifies as `SEND_UNKNOWN`, never as failure and never
as success; **no automatic retry after an ambiguous send**; `claim_owner_hash` ownership; the
fence acquired and released through the compiler's typed operation; final authorization
immediately before the send. **Acceptance by SES is not delivery, and this system does not claim
exactly-once email delivery.**

**Frozen event-destination transport: EventBridge.** The `chorus-demo` configuration set gets one
event destination publishing `Send`, `Delivery`, and `Rendering Failure` to the default event bus;
a rule on `source = aws.ses` targets the reconciliation entry point of the worker. EventBridge is
chosen over SNS because the event arrives with an AWS-attested envelope the
`SesEventTransportAuthenticator` can bind to a rule ARN, and because it needs no subscription
confirmation dance. Phase 11 owes that authenticator; with none, a `SEND_UNKNOWN` row stays
`SEND_UNKNOWN`, which ADR-025 § 9 already names as correct.

### 10.2 Inbound — one exact transport

**Frozen path — one S3 receipt action carrying its own `TopicArn`**
([ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) § 1):

```text
MX for {inbound-subdomain}  ->  SES inbound receiving (us-east-1)
receipt rule set  chorus-demo-inbound
  receipt rule    chorus-demo-reply        (recipient: the one receiving address)
    S3Action(
      BucketName      = chorus-private-evidence-demo
      ObjectKeyPrefix = ns/DEMO/inbound/
      TopicArn        = arn:aws:sns:us-east-1:{account}:chorus-demo-inbound-receipt
    )
  SNS topic  chorus-demo-inbound-receipt
  SNS subscription  ->  inbound Lambda entry point (worker artifact)
```

**One action, not two.** A rule with an S3 action *followed by* a separate SNS action publishes a
notification describing the **SNS** action, which carries no `bucketName` and no `objectKey` — the
attester would have nothing to fetch. Configuring the S3 action's own `TopicArn` is what makes the
notification describe the write that just happened. The attester **requires
`receipt.action.type == "S3"`** and refuses any other action type as `MALFORMED_ENVELOPE`.

**Why not plain S3 `ObjectCreated`.** It carries a bucket and a key and **no SES receipt
verdicts**. Using it as the trust event would delete
[ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md) § 2 step 4 outright.

**Exact pinning — the full receipt-rule ARN everywhere, never the rule-set ARN:**

```text
arn:aws:ses:us-east-1:{account}:receipt-rule-set/chorus-demo-inbound:receipt-rule/chorus-demo-reply
```

A rule-set ARN authorizes every rule in the set, including rules added later.

| | Value |
|---|---|
| Source account | the deployment account, asserted as `aws:SourceAccount` wherever supported |
| Rule | the **full receipt-rule ARN** above, held in `CHORUS_INBOUND_SOURCE_ARN` |
| Bucket / prefix | `chorus-private-evidence-demo` / `ns/DEMO/inbound/` |
| SNS topic policy | admits `Principal: ses.amazonaws.com` only, conditioned on `aws:SourceAccount` **and** `aws:SourceArn` = the full receipt-rule ARN |
| Lambda resource policy | admits `Principal: sns.amazonaws.com` conditioned on `aws:SourceArn` = the exact topic ARN; no other principal may invoke this entry point |
| Attester transport pair | `CHORUS_INBOUND_TRANSPORT` = `aws:ses-receipt`, `CHORUS_INBOUND_SOURCE_ARN` = the full receipt-rule ARN |

**Provenance comes from the exclusively authorized AWS delivery chain and deployment
configuration**, as specified by [ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) § 2.
The topic policy explicitly denies other publishers and wrong SES account/rule sources. The
Lambda subscription/resource policy admits only the exact SNS topic, and effective identity
permissions must not permit an alternate direct invocation of this inbound function.

The runtime receives a normal SNS event. It decodes the outer envelope, requires
`EventSource == "aws:sns"`, and checks `Records[i].Sns.TopicArn` against the separately configured
expected ingress topic ARN. It then decodes `Records[i].Sns.Message` as the SES notification and
requires the existing verdict gates, S3 action type, configured bucket/prefix, and the matched
receiving address using `receipt.recipients` and the configured digest. Existing object pinning,
correlation, attestation, and admission follow in ADR-030's order.

Python Lambda's context does **not** supply SNS TopicArn/source metadata. SES receipt JSON does
**not** contain a receipt-rule ARN. `CHORUS_INBOUND_SOURCE_ARN` is the full receipt-rule ARN from
trusted deployment configuration, enforced by SNS publish and bucket write policies and used
as expected SES provenance. The SNS event's TopicArn is a **consistency check**, not proof of
origin; the two ARNs are distinct and are not compared to each other. Under the exclusive
policy-constrained ingress path, the adapter constructs the logical transport context using
`aws:ses-receipt` and that configured rule ARN. Forging those strings in JSON grants no access.

The four-stage ADR-026 boundary and sender-authentication verdicts remain unchanged. Negative
publish/invoke canaries must prove that SNS-shaped JSON cannot bypass the resource graph.

**The object is pinned before it is parsed** ([ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md)
§ 3). The receipt-derived `bucketName`/`objectKey` is the only locator admitted, and it is
validated against the configured bucket and prefix before a byte is fetched. Then **one** bounded
`GetObject` reads at most the 256 KiB cap plus one byte; the response's `VersionId` and `ETag` are
captured from that same read — the receipt carries no `VersionId`, so the first trusted read is
what resolves and pins it. `sha256` is computed over exactly the bytes read, and **that one buffer
is what every downstream step consumes**: MIME parsing, plain-text extraction, the thread-header
fallback, the persisted hash and byte length, the evidence root, and the attestation. **There is
never a second read**, so the bytes that were hashed and the bytes that were parsed cannot differ.

Substitution resistance is a property of the bucket, not of timing: versioning is enabled, SES
writes under a service-generated key, the application principals are **denied** `s3:PutObject`,
`s3:DeleteObject`, and `s3:DeleteObjectVersion` under `ns/DEMO/inbound/*` (the inbound entry point
holds `s3:GetObject` there and nothing else), and only the reset principal and the lifecycle rule
may remove ingress objects. "The bucket and prefix matched" is explicitly not sufficient.

### 10.3 Inbound encryption and bucket policy

**SES receipt-rule "message encryption" is client-side encryption** performed with the AWS
Encryption SDK. An object written that way is *not* readable by the plain `GetObject` of § 10.2,
so enabling it would hand the attester ciphertext where it expects MIME.

**Frozen: SES receipt client-side encryption OFF.** The object is protected at rest by the
bucket's default SSE-KMS with the private evidence key, exactly like every other private object.
Block Public Access in all four forms, bucket-owner-enforced ownership, `enforce_ssl`, versioning,
and every cross-zone restriction are untouched.

**The compatibility problem.** The private bucket's `DenyUnencryptedObjectUploads` and
`DenyWrongKmsKey` statements are conditioned on request headers, and the SES receiving service
sends neither `x-amz-server-side-encryption` nor `…-aws-kms-key-id` — it relies on bucket default
encryption. Both denies would block SES's own write, and **neither may be removed**: they are the
guarantee protecting the one bucket that holds raw private evidence.

**The carve-out is six separate statements**, specified as valid JSON in
[ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) § 7:

1. `AllowSesReceiptWriteToInboundPrefixOnly`: direct SES service, exact account, full receipt-rule
   ARN, and ingress prefix; `s3:PutObject` only.
2. `DenyNonSesWrongOrMissingEncryption`: every non-SES writer must supply `aws:kms`.
3. `DenyNonSesWrongOrMissingKmsKey`: every non-SES writer must supply the exact private CMK ARN.
4. `DenySesWrongOrMissingSourceAccount`: SES with a wrong or absent account is explicitly denied.
5. `DenySesWrongOrMissingReceiptRule`: SES with a wrong or absent full rule ARN is explicitly denied.
6. `DenySesOutsideInboundPrefix`: SES is explicitly denied outside the exact ingress prefix.

The S3Action uses the direct SES service principal, not an optional delivery role.
`aws:PrincipalServiceName` distinguishes that principal from ordinary IAM writers and other
services. Statements 2–3 intentionally AND `non-SES` with one invalid encryption header.
Statements 4–6 are independent: matching the account cannot suppress a wrong-rule Deny.
Together they permit a headerless exception **only** for the exact SES service/account/rule/prefix.
Request JSON cannot set IAM context keys. The existing TLS, cross-zone, and application ingress
write/delete Denies remain; the private key's SES grant uses the exact account/full rule ARN.

**All nine cases in ADR-030 § 7 are mandatory independent canaries (K2 in § 15):** exact SES
without request encryption headers; authorized application with correct algorithm/key; missing
key; wrong key; same-account wrong SES rule; wrong SES account; same-account non-SES headerless
ingress attempt; correct SES path with wrong prefix; and forged event/JSON fields. The first two
are allowed under their existing permissions; all others are denied, with explicit S3 Denies
for cases 3–8 and the effective publish/invoke boundary for case 9. Also test missing/wrong
algorithm and missing SES source keys. Never use absence of another Allow as the proof of an
encryption exception's scope.

**A transport ingress object is not admitted evidence.** SES writes the raw MIME *before* the
attester judges anything, so objects exist under `ns/DEMO/inbound/` for deliveries later refused
for a foreign source, a failed verdict, a missing correlation, an attachment, an oversize body, or
a malformed envelope. A rejected ingress object **never** enters case evidence — no `EvidenceItem`,
no `EvidenceRoot`, no reference from any case partition — and **never** advances a commitment, an
extraction, a case state, or an authorization version. It remains transport data only, accounted
for by the 30-day private lifecycle rule and by the reset principal's bounded `ns/DEMO/` prefix
deletion (§ 12), and by nothing else. Each refusal still emits exactly one `reply.rejected` audit
event carrying a closed reason code and no content.

## 11. Live commitment extraction

[ADR-027](../adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 38 assigns
`EXTRACT_COMMITMENT` to the **Investigator** runtime under its own prompt version
`commitment-extraction/v1`. The deployed Investigator artifact now serves it: `handle`
dispatches on a declared operation, and the allowlist carries the extraction contract and its
reviewed prompt. The local composition still uses `LiteralSpanCommitmentExtractor` — a stand-in,
not a fallback — because the deployed composition root is I2 and is not yet written.

**Frozen: a second bounded operation in the same Investigator runtime. No fourth agent.**

| | Value |
|---|---|
| Wire contract | `investigator-request/v1` — `{schema_version, operation, invocation}`, Investigator-only |
| Discriminator | the envelope's **explicit** `operation` field: `INVESTIGATE` \| `EXTRACT_COMMITMENT` |
| Contracts | `CommitmentExtractionInput` / `CommitmentExtractionOutput`, `commitment-extraction/v1` — already written and tested |
| Prompt version | `commitment-extraction/v1`, pinned in the artifact beside `investigator/v1` |
| Dispatch | one branch in `handle`, on the declared kind only, selecting prompt + output model. No branch on payload content |
| Artifact | `[artifact].include` gains `src/chorus/contracts/commitment.py` and `src/chorus/contracts/agentcore.py`; boundary tests extend to both |
| IAM | **unchanged** — the Investigator role, the Investigator profile, the Investigator log group |
| Limits | the extraction path uses the Investigator's existing budget/timeout rungs |
| Live evaluation | its own gated scenarios, run before acceptance (§ 20) — still `NOT_RUN` |

**Correction — the discriminator did not exist and had to be added.** The earlier draft named
"the request envelope's existing operation kind", but `AgentInputEnvelope` has no operation field
and `ApplicationOperationKind` never crosses the port. The discriminator is therefore a new
Investigator-only wrapper, `chorus.contracts.agentcore.InvestigatorRequest`, parsed as a Pydantic
**tagged union**: the tag is read first, exactly one member is selected, and an unrecognised or
absent tag is refused before either payload model is tried and before any model is reached. A
commitment payload declared as `INVESTIGATE` is refused rather than rerouted to the arm that
happens to fit. The Monitor and the Action runtime keep their bare `AgentInputEnvelope` bodies —
one operation each, so a discriminator there would be a constant a caller could only get wrong.

**The envelope guard is per-operation.** An investigation names one version of one case and its
envelope must carry both. An extraction is bound to one immutable inbound artifact, and the
application deliberately sends `case_id=None` / `case_version=None` for it (ADR-027 § 1) — the
case identifier it is about lives in the *payload*, where the output must echo it back. A single
stricter guard would reject the only extraction envelope the system ever sends.

**Amendment — live extraction receives the safe destination display label.**
`CommitmentExtractionInput` gains `destination_display_label`. ADR-027 § 3 check 4 requires
`normalize(obligor)` to equal the normalized safe `display_label` of the correlated destination,
and the frozen demo reply ("we will restore elevator b to service by 2030-01-14.") contains no
organization name at all — so a model given only the reply could satisfy check 4 only by
accident, and live extraction would fail on correct answers. The label is already classified a
**safe, non-secret environment variable** by § 13, beside the registry version and the routing
token; it names no mailbox and carries no address, token, or registry record. The local
`LiteralSpanCommitmentExtractor` had it out-of-band, which is exactly why the gap was invisible
locally.

*Supplying it does not move authority.* Check 4 still compares the model's restatement against
the value deterministic code holds, so the model can only agree with a fact the correlation
already established or be rejected; the prompt requires `obligor_span` to keep citing the reply's
own words for the speaker, so the proposal stays checkable against the reply as well as against
who wrote it. The label is rendered **outside** the untrusted-data fence, because it is
configuration rather than something the reply said. It is absorbed into `extraction_input_hash`,
so an extraction produced under one correspondent is not recovery proof for another: a replay
whose label has changed re-runs instead of replaying an answer that would now fail check 4.

Two prompts in one runtime is not two agents: one identity, one role, one profile, one log group,
one deployment. Validation, grounding, the nine deterministic checks, and the span-cited
authority all stay in application code exactly as ADR-027 froze them — the runtime returns a
strict schema and decides nothing.

## 12. Reset authority

Reset is a Phase 11 deliverable and the normal application roles cannot perform it. The API role
holds no `DeleteItem` on view prefixes, no `s3:DeleteObject` anywhere, and is explicitly denied
`scheduler:DeleteSchedule` by [ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md)
§ 6. Granting it those would make the request-path principal able to erase the system.

**Frozen: a separate narrow operational principal**, in the manner `02` § Principal-specific
constraints already contemplates for audit readers.

| | Value |
|---|---|
| Principal | `chorus-demo-reset-demo`, a dedicated Lambda role; **not** attached to the API or worker |
| Invocation | `uv run chorus-demo reset --namespace DEMO --confirm "RESET DEMO"` → the reset function. Not reachable from a browser route without the demo token *and* the confirmation string |
| Namespace bound | every grant carries `ForAllValues:StringLike dynamodb:LeadingKeys = ["NS#DEMO", "NS#DEMO#*"]` — the **delimiter-aware** grammar (review R3), so `NS#DEMO2` and `NS#DEMONSTRATION` match neither entry — and every S3 grant is scoped to `ns/DEMO/*`. The role **cannot name a partition or prefix outside `DEMO`**, in any table or bucket |
| Environment bound | the function refuses to run unless `CHORUS_ENVIRONMENT=demo` and `CHORUS_NAMESPACE=DEMO`; `production` is already rejected at startup in V1 |

**What it may touch** (implemented in `chorus.composition.deployed_demo_reset`; review R2):
Core and Shareable partitions in the `DEMO` namespace, enumerated from the **persisted
`DemoManifest`** (`NS#DEMO` / `DEMO_MANIFEST#{seed_version}`) — query-by-partition then bounded
batch delete, **never a table scan**, and a missing or corrupt manifest fails the reset closed;
Audit `DEMO`-namespace partitions the same way; `ns/DEMO/` prefixes in both evidence buckets,
**including the un-admitted inbound ingress objects** of § 10.3; schedules in group `chorus-demo`
whose names carry the `chorus-{env}-{namespace_hash8}` grammar (`scheduler:ListSchedules` +
`DeleteSchedule` scoped to that group); operation and idempotency records in the enumerated
partitions; the demo manifest, the reset lock, the reset receipts, and the demo clock row are
**preserved** by the purge (their sort-key prefixes are the reset control plane) — the manifest
is rewritten, the clock row is reseeded, never deleted.

**The clock reset is the one fenced step, and reset is the only legitimate backward-time
transition in the system** ([ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md) § 3).
Holding the `DEMO_RESET_LOCK` it already takes, it atomically advances `reset_generation`
monotonically and never reuses a value, restores `logical_time` to the frozen `seed_instant`,
zeroes `advance_count`, and begins a fresh `version` sequence. An advance still in flight from the
previous demo run carries generation `N` and therefore fails against generation `N+1` **even
if its numeric `version` happens to coincide with a live one** — which is precisely what version
alone could not promise, because a restarted sequence necessarily revisits low numbers. The reset
principal is the sole holder of clock write authority besides the API's forward-only CAS.

**What it retains:** the audit trail is re-seeded, not preserved, because the namespace is a demo
namespace and `02` § Environment behavior says reset deletes only the `DEMO` namespace. Every
other namespace, every table, and every bucket is untouched — reset deletes rows and objects,
never a table and never a bucket.

**Refusal rules, unchanged from the local implementation:** it refuses before deleting anything
if **any** `ActionExecution` in the namespace is `SENDING` or `SEND_UNKNOWN`; it takes the
`DEMO_RESET_LOCK` so two resets cannot interleave; it validates the fixture snapshot and the
idempotency key before any destructive step and rechecks its receipt under the lock; a replayed key returns the recorded receipt and
performs no second reset.

## 13. Configuration and secrets

| Value | Class |
|---|---|
| `CHORUS_ENVIRONMENT=demo`, `CHORUS_NAMESPACE=DEMO`, `CHORUS_AWS_REGION=us-east-1`, `AWS_REGION=us-east-1`, `CHORUS_LOG_LEVEL`, `CHORUS_POLICY_VERSION` | safe env var |
| `CHORUS_*_TABLE`, `CHORUS_*_BUCKET`, `CHORUS_SCHEDULER_GROUP`, `CHORUS_SCHEDULER_ENVIRONMENT`, `CHORUS_SES_CONFIGURATION_SET` | safe env var, from CDK output |
| `CHORUS_AGENT_MODE=agentcore`, `CHORUS_AGENT_TIMEOUT_SECONDS`, `CHORUS_DEMO_CLOCK_ENABLED`, `CHORUS_OTEL_ENABLED` | safe env var |
| `CHORUS_{MONITOR,INVESTIGATOR,ACTION}_RUNTIME_ARN` — the **endpoint** ARN | deployment output → env var |
| `CHORUS_{MONITOR,INVESTIGATOR,ACTION}_MODEL_PROFILE_ARN` — **discovered**, never constructed | deployment output → env var |
| `CHORUS_{SENDER,COMPILER,WATCHER,WORKER}_FUNCTION_ARN`, `CHORUS_SCHEDULER_ROLE_ARN`, `CHORUS_SES_IDENTITY_ARN`, `CHORUS_INBOUND_SOURCE_ARN` | deployment output → env var |
| **new** `CHORUS_PRIVATE_EVIDENCE_KEY_ARN`, `CHORUS_EXPORT_EVIDENCE_KEY_ARN` (§ 9) | deployment output → env var |
| `CHORUS_SES_FROM_IDENTITY_ID`, `CHORUS_DESTINATION_{ID,DISPLAY_LABEL,REGISTRY_VERSION,ROUTING_TOKEN}` | safe env var — **non-secret by design**, names no mailbox |
| `CHORUS_DESTINATION_ADDRESS_DIGEST`, `CHORUS_INBOUND_ADDRESS_DIGEST` | safe env var — digests, never addresses |
| `CHORUS_PUBLIC_BASE_URL` | safe env var (the CORS origin) |
| `CHORUS_DESTINATION_REGISTRY_SECRET_ARN` → the secret's **contents** | **Secrets Manager**, sender-only: verified destination address, from/reply-to addresses, identity ARN |
| `CHORUS_DEMO_ACCESS_SECRET_ARN` → the secret's **contents** | **Secrets Manager**, API-only: the demo access token hash |
| account ID, deploy region, resolved AZ names (§ 6) | CDK context / deploy config |

No secret is committed; `.env` is local-development only and is never deployed secret storage.
`tools/check_secrets.py` runs in CI. **`.env.example` is stale** — it omits
`CHORUS_SCHEDULER_ENVIRONMENT`, `CHORUS_INBOUND_TRANSPORT`, `CHORUS_INBOUND_SOURCE_ARN`,
`CHORUS_DESTINATION_ADDRESS_DIGEST`, and `CHORUS_INBOUND_ADDRESS_DIGEST`, all of which exist in
`Settings`; the two key ARNs are new. All are added with safe placeholders.

## 14. API, worker, and frontend deployment

**Host: API Gateway HTTP API (payload format 2.0) + FastAPI on Lambda**, plus a separate
application-worker Lambda. Frozen by [08-api-design.md](../architecture/08-api-design.md).

**API entry point — built in Phase 11 batch 4** (`functions/api/`):

- the **ASGI adapter** is `mangum`, pinned in `pyproject.toml`, with `lifespan="off"` (the
  application is built around an explicitly constructed container, so no start-up hook exists to
  run). It handles **payload format 2.0** — `requestContext.http`, `rawPath`, `rawQueryString`,
  single-valued `headers`, the `cookies` array — and payload-v2 contract tests drive the real
  binding with real events;
- **`application/problem+json` is added to Mangum's text media types.** It is not in its default
  list, so without that every 401, 404, 409, 422 and 503 — the whole frozen error contract —
  would reach a browser base64-encoded while the happy path looked perfect;
- **bearer demo-token validation** against Secrets Manager, as ASGI middleware so it applies
  identically to a deployed request and to a contract test. The secret holds the token's
  **hash**; the presented token is hashed and compared with `hmac.compare_digest`. Missing token,
  wrong token, and an unreadable secret are **one** response shape, and an unreadable secret
  fails **closed**. The check is installed only when the composition supplies a verifier, so a
  deployment without one has no check rather than a permissive one;
- **persona header handling** unchanged — `X-Chorus-Demo-Actor` selects one fixed seeded persona
  **after** token validation, in the deployed demo exactly as locally;
- **one authoritative logical instant per request.** The deployed clock is a durable row, not a
  Python object, and a `Clock` answers synchronously, so the row is read once per request —
  strongly consistent — and bound for that request's duration. A clock that is missing, corrupt,
  or unreachable is a typed `503` and no work, never a fallback (ADR-029 § 4);
- `Cache-Control: no-store` and `X-Correlation-Id` on every response, refusals included;
- **no localhost DynamoDB default** — `api_settings` refuses to construct when
  `CHORUS_DYNAMODB_ENDPOINT` is set, rather than trusting the default.

**The deployed API does not compile and does not run agents.** Its role is denied every write on
the view prefixes, so the compile route invokes the compiler function synchronously
(`compile-request/v1`); it holds no `bedrock-agentcore:InvokeAgentRuntime`, so every
agent-invoking route dispatches to the worker and returns `202`.

**Worker entry point — built in Phase 11 batch 4** (`functions/worker/`): the **durable
dispatcher** is `InvocationType="Event"` against the one configured worker ARN, replacing
`InProcessOperationDispatcher`. **No `BackgroundTasks` substitute**: an operation that lives in a
request process dies with it. The handover contract is `worker-job/v1` — identifiers, versions,
digests and instants, and **no message text, projected payload, agent output, or request
object**; the kind is read from the envelope's declared field and an unknown one fails closed
before anything is loaded.

Async delivery may repeat, and **nothing in the handler tries to prevent that**: a process-local
seen-set is an answer to a cross-process question. The operation/input hash and the conditional
`PENDING→RUNNING` claim are the duplicate-execution boundary, and `SEND_ACTION` is protected more
strongly still by the execution's own `APPROVED@v → SENDING@v+1` compare-and-swap. A test
delivers one identical event twice through the production handler over the real worker and
asserts the model was invoked exactly once.

**Compiler, sender, and watcher entry points — built in Phase 11 batch 4.** Each binds its
existing composition root to real AWS clients and adds nothing else:

| Function | Operations | Notes |
|---|---|---|
| `functions/compiler/handler.py` | `CompileView`, `AcquireSendAuthorizationFence`, `ReleaseSendAuthorizationFence` | the sole creator of views and the sole send authority; no model, no mail, no scheduler |
| `functions/sender/handler.py` | `SendAction` | `send-action-request/v1`; the failure *kind* travels with its safe code so an ambiguous outcome stays ambiguous and `SEND_UNKNOWN` stays a quarantine |
| `functions/commitment_watcher/handler.py` | `RecordCommitmentDue` | one `commitment-watcher-request/v1` for **both** callers — Scheduler and the demo-clock route reach the identical use case |

Every handler builds its object graph **lazily on first invocation**, so importing one needs no
credentials, no configuration, and no network; a test imports all five with AWS credential
resolution disabled. Every refusal body is a reason code and nothing else: no traceback, no
payload echo, no downstream body.

**The inbound-mail entry point is not built.** It is [ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md)'s
and belongs to a later batch; the deployed container wires `inbound_replies=None`, so the route
answers `503` rather than accepting an unauthenticated delivery.

**Frontend hosting is a conditional, not a deferral.** Two branches, and the hackathon rules
decide which (P4):

- **If a locally-run SPA against the deployed API is acceptable:** Phase 11 builds no web stack.
  `CHORUS_PUBLIC_BASE_URL=http://localhost:5173`, CORS admits exactly that origin, and
  `README`/status say plainly that the frontend runs locally against deployed AWS.
- **If judging requires a public URL:** `AmbientChorusWeb` is **restored into Phase 11** and must
  deploy before acceptance — private S3 bucket + CloudFront with OAC, SPA 404→`index.html`
  fallback, HTTPS only, `no-store` on `index.html` and immutable hashed assets, and **no secret in
  any `VITE_*` variable**. `CHORUS_PUBLIC_BASE_URL` becomes the distribution domain.

This must be resolved before the deployment gate, not discovered at submission.

## 15. Live canary matrix

Post-deploy, before the hero smoke, non-destructive.

| # | Canary | Expected |
|---|---|---|
| A | Worker role: `PutItem`/`GetItem` on `chorus-core-demo` in `NS#DEMO` | **Allowed** |
| B | Compiler role: compile one allow-case end to end | **Allowed**; safe object written **with the exact `SSEKMSKeyId`** (§ 9); transaction commits; view hash matches |
| B2 | Compiler role: `Query` on Shareable `NS#*#ACTION#*` (§ 8.2) | **Allowed** |
| C | Compiler role: `PutItem` on Core `NS#DEMO#CASE#…` | **AccessDenied** |
| D | Compiler role: `bedrock:InvokeModel` | **AccessDenied** |
| E | Sender role: `bedrock:InvokeModel`, `bedrock-agentcore:InvokeAgentRuntime` | **AccessDenied** (both) |
| E2 | Sender role: `GetItem` on `chorus-core-demo` | **AccessDenied** (total Core deny) |
| F | Action runtime role: `GetObject` on private evidence; `Query` on Core | **AccessDenied** (both) |
| F2 | Monitor and Investigator roles: the same two | **AccessDenied** |
| F3 | Each agent role: `GetObject` on **its own artifact prefix** (§ 6) | **Allowed** — proves the deny split did not re-break cold start |
| G | Full approved path → one `sesv2:SendEmail` → `SENT` + message ID | **Allowed**, exactly one attempt |
| H | `SEND_UNKNOWN` simulation | row is `SEND_UNKNOWN`, **no locator written**, **no automatic retry** |
| I | `CreateSchedule` → watcher fires → stale event = no-op; eligible = one `PENDING→DUE` | both; the CAS runs once |
| I2 | Worker role: `scheduler:DeleteSchedule`; watcher role: `scheduler:GetSchedule` | **AccessDenied** (both) |
| I3 | Watcher role: strong read of `NS#DEMO#CLOCK`; then any write to it (ADR-029 § 2) | **Allowed**, then **AccessDenied** |
| I4 | Advance carrying a stale `reset_generation` after a reset bumped it, with a coinciding `version` (ADR-029 § 3) | **refused** — the generation fence, not the version, is what rejects it |
| I5 | Clock row deleted, then a watcher invocation and an advance | both **fail closed and typed**; no fallback to a process-local clock, to `SystemClock`, or to the event timestamp |
| J | `InvokeAgentRuntime` on each of the three runtimes with a harmless schema fixture | three valid schema responses — proves cold start, VPC egress, and cross-region profile invocation together |
| J2 | Monitor role attempting to invoke the Investigator runtime | **AccessDenied** |
| J3 | `EXTRACT_COMMITMENT` against the Investigator runtime (§ 11) | valid `commitment-extraction/v1` output |
| K | Inbound event with a foreign `source_arn`; one with `dmarcVerdict=FAIL`; one whose `receipt.action.type` is not `S3`; one whose `bucketName`/`objectKey` fall outside the configured bucket and prefix | refused: `FOREIGN_TRANSPORT_SOURCE` / `INBOUND_VERDICT_FAILED` / `MALFORMED_ENVELOPE` ×2; nothing persisted; one `reply.rejected` audit row each; **the ingress object, where one exists, remains un-admitted transport data** |
| K2 | The nine inbound policy cases 1–9 in ADR-030 § 7, referenced by § 10.3, each exercised independently | cases 1–2 allowed; cases 3–9 denied by the stated boundaries |
| K3 | Inbound entry-point role: `PutObject` and `DeleteObject` under `ns/DEMO/inbound/*` (§ 10.2) | **AccessDenied** (both) — ingestion cannot replace the bytes it reasons about |
| L | **The correlation canary.** Real reply from the verified correspondent identity to the receiving address | `SendEmail` `MessageId` → delivered `Message-ID` local part → reply's `In-Reply-To`/`References` (read per [ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) § 4, from `mail.headers` or the pinned bytes) → **exactly one direct locator lookup**, no scan, no GSI; the pinned raw MIME lands private; commitment extracted |
| M | Sentinel sweep of CloudWatch logs and X-Ray traces | the injected secret string **absent everywhere** |

No destructive or chaos testing in Phase 11 — Phase 12 owns the adversarial matrix.

**Canary L is the one that can invalidate a design rather than a policy.** If the `Message-ID`
relationship does not hold, or if replies do not echo `References`, the correlation contract
needs an explicit ADR amendment **before** the demo. Custom headers and SES message tags are
assumed **not** to survive a reply and are not depended on.

## 16. Deployment DAG

Derived from actual CDK dependencies, with **artifact build separated from artifact publish** —
the bucket must exist before anything is uploaded to it, and every function must exist before a
consumer's policy can name its ARN.

```text
(0)  identity check (§ 2)  ->  cdk bootstrap, pinned execution policy  [once]
        v
(1)  AmbientChorusNetwork        VPC, 2 isolated subnets (resolved AZ names), no IGW/NAT,
        |                        6 interface + 2 gateway endpoints, security groups
        v
(2)  AmbientChorusData           3 tables, 2 evidence buckets, 2 KMS keys,   [RETAIN]
        |                        + chorus-agent-artifacts-demo
        v
(3)  artifact BUILD              per-runtime allowlist -> Linux-targeted deps -> main.py -> zip
        |                        (offline; no AWS)
        v
(4)  artifact PUBLISH            upload {agent}/{sha256}.zip to the artifact bucket
        |                        (needs (2); produces the S3 locations (5) consumes)
        v
(5)  AmbientChorusAgents         3 application inference profiles, 3 Runtimes, 3 endpoints
        |                        -> outputs: runtime ARNs, endpoint ARNs, DISCOVERED profile ARNs
        v
(6)  AmbientChorusCompiler       compiler Lambda (VPC)      -> output: function ARN
        v
(7)  AmbientChorusSender         SES domain + correspondent identities, config set,
        |                        event destination, sender Lambda (VPC, needs (6)'s ARN)
        v
(8)  AmbientChorusWatcher        watcher Lambda; scheduler execution role policies
        |                        (need the watcher ARN and the DLQ, § 8.5)
        v
(9)  AmbientChorusApplication    HTTP API, API Lambda, worker Lambda, demo-token secret,
        |                        two split roles whose grants name (5)(6)(7)(8) outputs
        v
(10) AmbientChorusInbound        receipt rule set + rule, S3 prefix policy, SNS topic,
        |                        inbound entry point + authenticator
        v
(11) AmbientChorusReset          reset function + narrow reset role (§ 12)
        v
(12) AmbientChorusObservability  dashboards, alarms, retention, OTEL content filter
        v
(13) canaries (§ 15)  ->  chorus-demo reset  ->  live hero flow (§ 17)
```

Stages (6), (7), and (8) are independent of one another and may deploy in parallel; every one of
them must precede (9), whose two roles name their function ARNs. (5) must precede (9) for the
same reason. The existing watcher-before-application ordering in `infra/cdk/app.py` is preserved
and generalised: **a principal's stack deploys after every resource its policy names.**

## 17. The live hero flow

**Real AWS for all of it:** real AgentCore invocations of all three runtimes against real Nova 2
Lite through the US inference profile, including live `EXTRACT_COMMITMENT`; the real compiler
Lambda; real DynamoDB, S3, and KMS; a real EventBridge schedule firing the real watcher against
the real durable clock; one real SES send; and a **real inbound SES reply** authenticated by real
SPF/DKIM/DMARC verdicts.

Exactly two things are fixtures, and neither is a fake service:

- **the synthetic ambient feed** — [ADR-005](../adr/ADR-005-synthetic-ambient-adapter.md), a
  frozen product decision;
- **the manager's reply text** — the words are scripted, but the message is a genuine email sent
  from a verified identity through real SES, delivered through a real receipt rule, authenticated
  by verdicts SES computed. The content is staged; the transport, authentication, and correlation
  are not.

**Nothing else may be fixture-backed**, and the deployed demo cannot silently fall back to the
local one: `demo` requires `agent_mode=agentcore`, `LocalInboundMailAuthenticator` refuses to
construct outside `test`/`development`, and the local sender is filesystem-only. **Do not claim
"live end-to-end" about any component running against a local fake.**

## 18. Cost and retention

**Corrected arithmetic.** The endpoint inventory is **6 interface + 2 gateway** (§ 7), not the
nine interface endpoints the earlier draft implied by listing gateways in the same table and then
counting eleven.

Interface endpoints are billed **per endpoint, per AZ, per hour**. With two AZs:
6 × 2 × 730 h × ~$0.01 ≈ **$88/month**, accruing whether or not anybody demos. Gateway endpoints
are free. This is the largest fixed line item.

**No-NAT is chosen because [ADR-010](../adr/ADR-010-agentcore-runtime.md) freezes the egress and
isolation boundary.** It also happens to be cheaper than a NAT gateway; that is a side effect, not
the reason, and the earlier draft had the justification backwards.

Everything on the demo path is pay-per-request and rounds to well under a dollar per run:
DynamoDB on-demand, Lambda invocations, AgentCore per-invocation, six to eight Nova 2 Lite calls,
one SES message, a handful of one-time schedules, kilobytes of S3.

**Idle and retained costs, which do not stop when the demo does:**

| Item | Note |
|---|---|
| 6 interface endpoints × 2 AZ | ~$88/month — destroy after judging |
| AgentCore runtimes | per-invocation, but a warmed/idle session can hold billable memory; verify and keep sessions short |
| 3 KMS keys (private, export, DLQ) | ~$1/month each, and **retained keys keep costing after `cdk destroy`** |
| 2 Secrets Manager secrets | ~$0.40/month each |
| CloudWatch logs | 14-day retention; the retention setting is what bounds this |
| DynamoDB PITR + storage | PITR is billed on table size; on for `demo` because it is non-disposable |
| Retained buckets and tables | survive stack deletion by design (below) |

A budget alarm and `Project=ambient-chorus` cost-allocation tags go in with the **first** deploy,
not after the bill.

**Retention and teardown — stated accurately.**

- **`RemovalPolicy.RETAIN` means CloudFormation leaves the resource behind.** It is not deletion
  protection and it is not a backup. `cdk destroy AmbientChorusData` **succeeds**, removes the
  stack, and orphans the tables, buckets, and keys — which then cost money and are no longer
  managed by any stack.
- **PITR is backup and recovery, not deletion protection.** DynamoDB `deletion_protection` (on,
  because `demo` is non-disposable) is what refuses a table delete.
- **`cdk destroy` is therefore not a complete cleanup.** Orphaned tables, buckets, keys, and
  secrets must be removed by hand, deliberately, after judging.
- **Retained names block redeployment.** `chorus-core-demo` and `chorus-private-evidence-demo` are
  fixed names; a fresh deploy into the same account and region fails with "already exists" while
  the orphans live. Two options, chosen in advance: **delete the orphans** before redeploying, or
  **deploy under a new environment token** (`-c environment=demo2`), which changes every resource
  name, ARN, and env var consistently because they are all derived from it.
- **Never destroy the Data stack during demo recovery.** Recovery is redeploying compute and
  repointing endpoint versions. The teardown runbook and the recovery runbook are different
  documents and the Data stack appears only in the first.

## 19. Observability

Structured CloudWatch JSON logs, split by API / worker / compiler / sender / watcher / each agent
runtime, 14-day demo retention. Allowlisted fields only, per
[09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md):
`correlation_id`, `causation_id`, `operation_id`, `invocation_id`, `execution_id`,
`commitment_id`, `case_id`, `case_version`, `authorization_version`, reason codes, outcome,
duration, attempt, `aws_request_id`, and `ses_message_id` (sender-only, after acceptance).

**Forbidden everywhere:** raw message/report/evidence text, prompts, completions, health/unit/name
values, email addresses and headers, presigned URLs, S3 keys, mandate terms, rendered bodies,
tokens, secrets. Strands/OTEL content capture disabled; the exporter processor drops
`gen_ai.prompt`, `gen_ai.completion`, message content, and tool arguments before export.
Canary M is the enforcement.

Alarms: the existing DLQ-depth alarm, plus Lambda error-rate and AgentCore invocation-failure
alarms. One dashboard. No monitoring platform.

## 20. Phase 11 acceptance

Phase 11 is **not** weakened. It is accepted only when all of the following hold:

- live evaluation of **Monitor**, **Investigator**, and **Action** against the real model;
- live **commitment extraction** (§ 11) against the real model;
- deployed compiler, sender, and watcher, each proved by its canaries;
- real AWS persistence — DynamoDB, S3, KMS — with the § 9 write repair proved by canary B;
- one **authenticated inbound reply** correlated to one `SENT` execution (canary L);
- **durable reset** (§ 12) run successfully and repeatably;
- **rollback proof** — an AgentCore endpoint repointed to a previous version and a Lambda alias
  rolled back, both exercised;
- the full **IAM and network canary matrix** (§ 15) green;
- the **alarms and dashboard** the implementation plan lists, working;
- **one real AWS hero flow** (§ 17) completed end to end.

Phase 12 remains broad evaluation, adversarial, and concurrency work. Phase 13 remains rehearsal,
presentation, submission, and cost cleanup.

## 21. Status

### User prerequisites — these block deployment and nothing else

| | Item | Resolution |
|---|---|---|
| **P1** | **Deployment identity.** CLI session expired; no verifiable non-root principal. | Create the `ChorusDeployer` permission set, `aws sso login`, confirm the ARN is not root. Blocks *deployment*, not offline implementation. |
| **P2** | **DNS control for a receiving domain or subdomain.** | Required: the ability to publish SES verification records and an **MX** record for the receiving subdomain, and DKIM records for the sending identity. **Not** required: buying a new domain if suitable DNS is already controlled; Route 53 specifically; owning both correspondents' domains; a custom MAIL FROM. **Hard prerequisite before any real inbound SES acceptance.** |
| **P3** | **Bedrock model access and region enablement.** | Confirm `us.amazon.nova-2-lite-v1:0` is invocable and that `us-east-1`, `us-east-2`, and `us-west-2` are enabled and unrestricted by SCP for `bedrock:InvokeModel` (§ 4). Account prerequisite; proved by canary J. |
| **P4** | **Hackathon frontend rule.** | Decide whether a locally-run SPA against the deployed API is acceptable. If not, `AmbientChorusWeb` returns to Phase 11 scope (§ 14). |

### Reclassified from the previous draft

| | Was | Now |
|---|---|---|
| B1 identity | blocker | **P1 — user prerequisite for deployment**, not for implementation |
| B2 region | blocker | **configuration issue** — pin the region; the CLI default is drift |
| B3 model | blocker | **P3 + live-canary question** (canary J) |
| B4 domain | blocker | **P2 — user prerequisite**, wording corrected (DNS control, not domain purchase) |
| B5 server binding | blocker | **implementation blocker** (I1) |
| B6 handlers/composition | blocker | **implementation blocker** (I2) |
| B7 authenticators | blocker | **implementation blocker** (I3) |
| B8 model IAM | blocker | **implementation blocker** (I4), scope corrected to inference-profile + conditioned FM ARNs |
| B9 role split | blocker | **implementation blocker** (I5) — confirmed |
| B10 SES production access | blocker | **NOT REQUIRED** for a verified sandbox demo; optional and desirable |
| B11 `Message-ID` | blocker | **live-canary question** (canary L); ADR amendment only if it fails |

### Implementation blockers — offline work, no AWS account needed

| | Item |
|---|---|
| ~~**I1**~~ | **Done.** AgentCore server binding: a bare ASGI application on the already-locked `uvicorn` rather than a `bedrock-agentcore` dependency, `main.py` with `/ping` + `/invocations` per runtime, the archive import bootstrap, `deployed_name`, and the corrected `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` names (§ 5). Live evaluation remains `NOT_RUN`. |
| **I2** | **Handlers: six of six done** (§ 14): API (Mangum, payload v2, bearer-token middleware, per-request logical time), operation worker, compiler, sender, commitment watcher, and — since Macro A — the operator-only demo-reset handler (`functions/demo_reset/handler.py`, thin: env gate, `demo-reset-request/v1` envelope, `DeployedDemoReset.reset`, typed error translation) — handlers, composition roots, the `worker-job/v1` async boundary, and the synchronous compile/fence/send/watcher contracts. **Resources: done offline (batch 5).** The five `Function` resources under the pre-existing roles/log groups (the reset `Function` is `AmbientChorusReset`, I13); the watcher published `Version` + `live` `Alias`; the API Gateway HTTP API with a `$default` payload-v2 proxy integration and a Lambda invoke permission scoped to that API and the API function alone; every cross-function ARN, the two API secret identities and the sender's, and the scheduler identity wired in `app.py`; the deterministic lock-based Lambda packaging (`tools/build_lambda_artifacts.py`) with its first-party secret gate, ELF check, and isolated-artifact import proof. **Macro A final completion repair:** `demo_reset` joined the canonical `FUNCTION_DIRS` build inventory — a normal build now produces **six** ZIPs (`api`, `worker`, `compiler`, `sender`, `commitment_watcher`, `demo_reset`), the reset ZIP passing the identical final-artifact gates with no exemption, and deployment-capable CDK resolution fails closed (`LambdaArtifactMissingError`) on a missing `chorus-demo-reset` ZIP with a deploy-guard test to prove it. **VPC attachment — done offline (Macro A, I16):** the worker, compiler, and sender `Function`s carry a `VpcConfig` on the isolated network's two subnets and their own security group; the API and watcher stay out. **Still owed:** the inbound-mail entry point (I9/I10, awaiting ADR-030). |
| **I3** | `InboundMailTransportAuthenticator` and `SesEventTransportAuthenticator` |
| **I4** | Agent IAM: discovered inference-profile ARNs + conditioned foundation-model ARNs (§ 4) |
| **I5** | Split API and worker roles; add the missing `lambda:InvokeFunction` and `secretsmanager:GetSecretValue` grants (§ 8.1). **Roles split in batch 4; batch 5 wires the conditional grants unconditionally** from the created worker resource, the compiler / sender / watcher-alias **actual resource ARNs** (cross-stack `Fn::ImportValue`), and the configured secret identities — a template test proves each function's environment identity and the matching IAM resource are one ARN. |
| **I6** | **Compiler Shareable read** — without it no send is possible (§ 8.2) |
| **I7** | **S3 `SSEKMSKeyId`** — without it no object write succeeds (§ 9) |
| **I8** | **AgentCore artifact deny split** — without it no runtime cold-starts (§ 6) |
| **I9** | SES receipt decoder corrections per [ADR-030](../adr/ADR-030-live-ses-receipt-decoding.md) — single S3 action + `TopicArn`, `receipt.action.type == "S3"`, one bounded pinned read reused everywhere, `mail.headers` with pinned-bytes fallback, `From` mailbox, `receipt.recipients` — with golden tests over **captured real** payloads. *Awaits ADR-030 acceptance.* |
| **I10** | Inbound bucket/key policy carve-out (six statements, nine proved cases) and ingress-object accounting (§ 10.3). *Awaits ADR-030 acceptance.* |
| ~~**I11**~~ | **Done offline.** Live commitment extraction in the Investigator runtime (§ 11): `investigator-request/v1`, the `commitment-extraction/v1` prompt, and the safe destination label the deterministic obligor check needs. Not yet run against a real model. |
| **I12** | **Done.** `DynamoDbDemoClockStore` at the exact literal `NS#DEMO#CLOCK`: strongly consistent read, and one guarded forward compare-and-swap whose `version`, `reset_generation`, and strictly-earlier-`logical_time` fences are all condition expressions the table evaluates. Missing, corrupt, and unreachable each fail closed and typed, with no fallback to a process-local clock, to `SystemClock`, or to an event's timestamp. The normal adapter exposes **no reset and no reseed** — asserted by absence. **Macro A adds** the reset principal's separate `DynamoDbDemoClockResetStore`: a strong read (absent row is not a failure — ADR-029 § 6) plus one conditional reseed that advances `reset_generation` monotonically and never reuses it, restores `logical_time`/`advance_count`, and starts a fresh `version` sequence; a stale pre-reset advance then fails on the generation fence even when its numeric version coincides. |
| **I13** | **Macro A residual correction implemented and verified offline; independent micro verification pending.** The dedicated reset role/function retains exact DEMO namespace, object-prefix, ENI, and private reseed boundaries. [ADR-031](../adr/ADR-031-demo-reset-mutation-interlock.md) specifies the atomic reset-lock condition on every normal mutation, case-world registration with first case creation, discovery of every registered case and its descendants, and existing contextual idempotency reservations for external attempts. Reset refuses in-flight or unresolved side effects before purge, reconciles schedules from durable projections as well as the bounded list, rechecks receipts under lock, and salts seed transactions with a receipt identity derived from the persisted reset generation. The six-ZIP pipeline packages and validates the frozen fixture resources; the one synthetic binary is exact-path/digest pinned and byte-scanned by the shared scanner. Export evidence reset grants are delete/list only. No AWS deployment or canary is claimed. |
| **I14** | Scheduler execution role policies (§ 8.5) — done (batch 4). **Lambda VPC ENI grants (§ 8.6) — done offline (Macro A; review R1):** four inline statements on each of the worker, compiler, sender, and reset roles — `AllowCreateVpcEni` (`ec2:CreateNetworkInterface` on `*`, **no condition**: Lambda's Hyperplane creation is a service operation and a subnet condition there is ineffective, documented as an AWS service limitation); `AllowManageVpcEni` (`Delete`/`Assign`/`Unassign` on `*`, `StringEquals ec2:Subnet` = the two isolated subnet ARNs); `AllowDescribeVpcEni` (the two `Describe*` on `*`, unconditioned); and `DenyVpcEniFromFunctionCode` — `Effect: DENY` on all six actions, `ArnEquals lambda:SourceFunctionArn` = this exact function. **No `lambda:SourceFunctionArn` on any Allow:** AWS's service-side ENI management does not carry that key, so a `SourceFunctionArn` condition on the Allow makes VPC attachment fail; it belongs on the Deny, where it fires only for the function's own code. No `AWSLambdaVPCAccessExecutionRole`, no `ec2:*`; the API and watcher roles receive none. |
| **I15** | AgentCore artifact **build** done — lock-based, `aarch64-manylinux2014`/3.12, ELF-verified, with its own secret gate (§ 5). The **Lambda** artifact build is done too (batch 5): `tools/build_lambda_artifacts.py`, one zip per production function, `x86_64-manylinux_2_28`/3.12, first-party trees re-rooted so the deployed import path matches the repository, ELF-verified, first-party secret gate, and an isolated unpacked-archive import test. Still owed: artifact **publish** (both), the deploy CLI with the § 2 identity refusal, and AZ-ID resolution (§ 6). |
| **I16** | **Network, Reset, and Observability stacks — done offline (Macro A).** `AmbientChorusNetwork`: one VPC, exactly two `PRIVATE_ISOLATED` subnets in the two `-c network_availability_zones=<a>,<b>` deployment-config AZs (deployment mode fails closed without them; offline synth uses a clearly-named synthetic fixture), no NAT/IGW/EIP, no default route; the frozen **six interface** endpoints (`bedrock-runtime`, `bedrock-agentcore`, `lambda`, `secretsmanager`, `scheduler`, `email`) and **two gateway** endpoints (`s3`, `dynamodb`) on both isolated route tables; a per-endpoint security group admitting TCP 443 only from the workloads the frozen matrix lists, and per-workload security groups with `allow_all_outbound=False` whose only egress is 443 to those endpoint SGs and to the S3/DynamoDB managed prefix lists; scoped S3 and DynamoDB endpoint policies. **The S3 endpoint policy's AgentCore exception is bounded (review R4):** it is `s3:GetObject` on the AWS-documented regional service bucket pattern `arn:aws:s3:::acr-code-*-us-east-1-an/*`, conditioned on `aws:PrincipalServiceName = bedrock-agentcore.amazonaws.com` — **not** the previous `s3:GetObject` on `arn:aws:s3:::*/*` for every principal. The evidence-object statement also covers `s3:DeleteObject`/`DeleteObjectVersion` and a prefix-constrained `s3:ListBucket` so the reset role's own IAM is usable through the endpoint (review R5-A). `AmbientChorusReset` and `AmbientChorusObservability` per I13 and § 27–30. **Still owed:** the **Inbound** stack (I9/I10, awaiting ADR-030); the AgentCore-runtime alarms (Macro B). `.env.example` unchanged this macro — Macro A adds no new required non-secret variable (the AZ names are `-c` context / deploy config, not a `CHORUS_` setting). |

**I6, I7, and I8 are the three that make an otherwise complete deployment fail at runtime**, each
in a way that reads as an unrelated error: no send, no object write, no agent cold start.

### Unresolved architectural questions

0. ~~**What clock does the deployed compiler stamp a view with?**~~ **Resolved** (Phase 11
   batch 4 repair, § 8.10 D): the compiler reads the durable logical clock, read-only, the same
   shape as the worker's grant in § 8.9 B. A compiled view's `generated_at` and `expires_at` are
   now logical-clock instants, in the same domain as the case world they describe and the
   freshness check that later compares against them. The rejected alternative was an ADR
   statement that view lifetime is wall-clock everywhere.
1. **Does AgentCore fetch the S3 artifact under the execution role or under a service-owned
   mechanism?** § 6 assumes the execution role and repairs the deny accordingly. Canary F3 plus a
   real cold start (canary J) settles it. If it is service-owned, the artifact grant is
   unnecessary but harmless and the endpoint policy still matters.
2. **Does a real reply echo `References`?** Assumed yes for standard clients, depended on by the
   correlation contract, proved only by canary L. Sub-addressed `Reply-To` routing remains the
   recorded fallback and would reopen the Phase-8 preview binding.
3. **Is `bedrock-agentcore` (data plane) reachable from an isolated subnet via a VPC interface
   endpoint in `us-east-1`?** Assumed yes; if not, the worker moves out of the VPC, which it can
   do without weakening any boundary since it holds no private-object grant the API does not.
4. **Does the AgentCore runtime session ID generator satisfy the service's minimum length?**
   Checked at first invocation (canary J), not assumed.
