# Trust zones, IAM, deployment, and configuration

**Phase 11 reset correction:** [ADR-031](../adr/ADR-031-demo-reset-mutation-interlock.md)
adds an exact Core `NS#DEMO` reset-lock absence condition to every normal DEMO mutation.
Sender/watcher still cannot read or write Core items; their total Core deny has only this
condition-only exception. Compiler receives the same condition capability. Existing role
descriptions below that say "no Core access" exclude this one reset interlock.

## Trust-zone diagram

```mermaid
flowchart LR
    subgraph Z1[1 Ingestion zone]
      Feed[Synthetic feed]
      API[FastAPI / application role]
    end
    subgraph Z2[2 Private investigation zone]
      Core[(Core table)]
      PS3[(Private S3)]
      M[Monitor runtime]
      I[Investigator runtime]
    end
    subgraph Z3[3 Deterministic policy boundary]
      C[Compiler Lambda]
    end
    subgraph Z4[4 Shareable zone]
      Share[(Shareable table)]
      ES3[(Export S3)]
    end
    subgraph Z5[5 Action zone]
      A[Action runtime]
      V[Proposal validator]
      S[Sender Lambda]
      W[Commitment watcher]
    end
    subgraph Z6[6 External systems]
      SES[SES]
      PM[Property manager]
      SCH[EventBridge Scheduler]
    end
    Feed --> API
    API --> Core & PS3 & M & I
    API -->|IDs, destination, purpose| C
    C --> Core & PS3
    C --> Share & ES3
    Share -->|immutable safe payload| A
    A --> V --> Share
    Share --> S --> SES --> PM
    API --> SCH --> W --> Share
```

Trust is directional. Data moving right is re-modeled into narrower types, not passed as a generic document. No route moves data from a later zone back into a private authorization decision without validation as untrusted evidence.

## IAM notation and resources

`R` = read/list/get, `W` = write/update, `I` = invoke the named runtime/function, `S` = external send, `—` = no allow, and `D` = explicit deny in the role boundary or resource policy. Explicit denies are used for the Action runtime and sender defense-in-depth; lack of an allow remains the default elsewhere.

| Principal | Core table | Share table | Audit table | Private S3 | Export S3 | Monitor runtime | Investigator runtime | Action runtime | Compiler | Sender | Scheduler | SES |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| FastAPI/application | RW | RW(action/execution/case)/R + CC(view)* | W | RW | R | I | I | I | I | I | W(create/get only)† | D |
| Monitor runtime | D | D | — | D | D | — | — | — | D | D | D | D |
| Investigator runtime | D | D | — | D | D | — | — | — | D | D | D | D |
| Compiler Lambda | R(all)/W(`FENCE` partition only) | R(all safe)/W(view only) | W | R | W | — | — | — | — | D | D | D |
| Action runtime | D | D | — | D | D | — | — | — | D | D | D | D |
| Sender Lambda | D except condition-only reset interlock (ADR-031) | R(all safe)/W(`EXECUTION` partition only) | W | D | D | — | — | — | I(fence API only) | — | D | S |
| Commitment watcher | D except condition-only reset interlock (ADR-031) | R/W(`NS#*#CASE#*` only) + R(`NS#DEMO#CLOCK` exactly)‡ | W | D | D | — | — | — | D | D | D | D |
| Scheduler execution role | D | D | D | D | D | — | — | — | D | D | — | D; invokes watcher only |
| Demo reset (operational) | RW(`NS#DEMO*` only) | RW(`NS#DEMO*` only) | RW(`NS#DEMO*` only) | RW(`ns/DEMO/` only) | RW(`ns/DEMO/` only) | — | — | — | — | — | List/Delete in `chorus-{env}` only | D |

`‡` The watcher's Core deny remains **total**. [ADR-029](../adr/ADR-029-deployed-demo-clock-authority.md) moves the demo logical clock out of the Core demo-manifest partition into its own Shareable partition, so the watcher can read the authoritative logical time its step-4 early-firing comparison requires without any Core grant and without gaining a single write. The grant names the **exact literal partition** `NS#DEMO#CLOCK` through `dynamodb:LeadingKeys`; there is no `NS#*#CLOCK*` form of it in any role, because a wildcard would authorize a clock in a namespace no deployment has. The watcher's read is strongly consistent and it holds no write action on that prefix in any form. The clock is a timestamp, not private data, and the shareable zone is where it belongs. The demo manifest and reset lock stay in Core, untouched. The **reset principal** below is the only other holder, and the sole legitimate source of a backward-time transition, fenced by the non-reused `reset_generation` of ADR-029 § 3.

The **demo reset principal** is an operational role in the manner of the audit reader below: it is not part of the application runtime, and no application role may hold its authority. The API role has no `s3:DeleteObject`, no view-prefix `DeleteItem`, and is denied `scheduler:DeleteSchedule` outright ([ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 6) — granting the request path the ability to erase the system is exactly what a separate principal avoids. Every one of its grants carries `dynamodb:LeadingKeys` = `NS#DEMO*` or an `ns/DEMO/` object prefix, so it can name nothing outside the demo namespace in any table or bucket, and it deletes rows and objects only — never a table and never a bucket.

`†` The application's Scheduler grant is `scheduler:CreateSchedule` and `scheduler:GetSchedule` on `arn:…:schedule/chorus-{env}/*`, plus `iam:PassRole` on the scheduler execution role alone. **No `DeleteSchedule` and no `UpdateSchedule`** — `DeadlineSchedulerPort` has no method for either, V1 has no reschedule verb, and a grant wider than its caller is a grant waiting for a second caller ([ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 6).

The commitment watcher's row was corrected by [ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 6. It read `Share: R/W(commitment/case projection)`, which mislocated the case row: the case lives in **Core**, which the watcher is denied outright, and the commitment, its schedule projection, and the verification-request item all live in the Shareable `NS#n#CASE#k` partition. The watcher takes no case edge in either table — `ACTIONED→VERIFYING` happened at commitment creation — so `LeadingKeys` scoped to `NS#*#CASE#*` on the Shareable table is its complete data-plane authority. It holds no `bedrock:*`, no `ses:*`, and no `scheduler:*`: it is a schedule *target*, never a schedule client.

The **inbound reply entry point is not a new principal.** It is an additional entry point of the existing application worker artifact, which already holds the Core read/write and private-S3 grants that persisting an `EvidenceItem` requires. Introducing a fourth principal needing the identical case-partition grant would buy no isolation — `LeadingKeys` cannot separate `EVIDENCE#` from `FACT#` on a sort key, the defect [ADR-019](../adr/ADR-019-send-fence-partition-isolation.md) and [ADR-024](../adr/ADR-024-execution-partition-and-sender-boundary.md) each found once — and would add a role to audit. What Phase 9 adds instead is a **composition-level** deny asserted by static test: the inbound composition root constructs no SES port, no Bedrock client, no compiler client, and no scheduler client. IAM already denies the worker SES.

`*` `CC` = `dynamodb:ConditionCheckItem`, read-only transactional authority. The application may create proposals, approvals, executions, commitments, and read views. Shareable-table partition keys begin with distinct `NS#...#VIEW#`, `VIEW_CURRENT#`, `ACTION#`, `ACTION_CURRENT#`, `EXECUTION#`, and `CASE#` prefixes. IAM `dynamodb:LeadingKeys` allows compiler writes only to the two view prefixes, application writes only to action/execution/case prefixes, and sender writes only to the execution prefix; the application therefore cannot create or mutate a view, and the sender cannot mutate the proposal or the approval it is about to honour ([ADR-024](../adr/ADR-024-execution-partition-and-sender-boundary.md)).

The application and the sender both write the `EXECUTION#` prefix, and that is the one Phase-8 boundary IAM does not draw. What separates them is the state machine: the application can only move a row in `DRAFT` or `APPROVED`, the sender only one in `APPROVED` or `SENDING`, and each write is conditioned on an exact row version. The single overlapping state is the approval-withdrawal race, which the compare-and-swap resolves with exactly one winner. This is stated rather than implied because it is asserted by a test over the transitions, not by a policy assertion. Conditions/repository invariants further protect immutable entity types, and CloudTrail tests the principal identity.

The application additionally holds `dynamodb:ConditionCheckItem` on `NS#*#VIEW_CURRENT#*`, scoped by `LeadingKeys` and preferably narrowed further with `dynamodb:EnclosingOperation` equal to `TransactWriteItems`. The action-proposal transaction must be able to condition on the exact current view without being able to move it ([ADR-022](../adr/ADR-022-action-draft-preview-and-transaction.md) § 7). **A condition check must never become a write grant**: no `PutItem`, `UpdateItem`, or `DeleteItem` is granted to the application on either view prefix, and a static negative-capability assertion over the synthesized policy proves it, in the manner [ADR-019](../adr/ADR-019-send-fence-partition-isolation.md) established for the compiler's read-only case guard.

Supporting-resource permissions are explicit as well:

| Principal | Bedrock inference profile | Own logs/traces | Demo access secret | Destination address secret | Private/export evidence KMS keys | Async Lambda invoke |
|---|---|---|---|---|---|---|
| FastAPI | DENY | WRITE | READ token hash | DENY | encrypt/decrypt private; decrypt export through evidence adapter | INVOKE worker/compiler only |
| Application worker | DENY | WRITE | DENY | DENY | same scoped evidence operations as application use case | INVOKE three agents/compiler/sender only |
| Monitor runtime | INVOKE Monitor profile only | WRITE own group | DENY | DENY | DENY | DENY |
| Investigator runtime | INVOKE Investigator profile only | WRITE own group | DENY | DENY | DENY | DENY |
| Compiler Lambda | DENY | WRITE own group | DENY | DENY | decrypt private; encrypt export | DENY |
| Action runtime | INVOKE Action profile only | WRITE own group | DENY | DENY | DENY | DENY |
| Sender Lambda | DENY | WRITE own group | DENY | READ exact destination secret | DENY | INVOKE compiler fence operation only |
| Commitment watcher | DENY | WRITE own group | DENY | DENY | DENY | DENY |
| Scheduler execution role | DENY | service delivery metrics only | DENY | DENY | decrypt DLQ key only | INVOKE watcher only |

KMS key policies repeat these principal/resource constraints; possessing an S3/DynamoDB action without the required key action is insufficient. Safe destination label/version/routing token and `from_identity_id` are deployment configuration, not Secrets Manager reads by agents/compiler. `from_identity_id` is an opaque stable identifier for the verified sending identity and is never the `From` address; only the sender resolves the address, from its own secret.

All three agent runtime roles have only:

- `bedrock:InvokeModel`/`InvokeModelWithResponseStream` on that agent's application inference profile;
- CloudWatch log and OTLP/X-Ray emission to its own log group;
- ECR/S3 artifact bootstrap permissions required by AgentCore, scoped to its artifact;
- KMS decrypt only if required for the artifact/log key.

They have no general network tool and no persistent AgentCore filesystem or Memory. All use Python 3.12 direct-code artifacts and AgentCore VPC network mode in two isolated subnets with no internet gateway/NAT route. The runtime security group permits TCP 443 only to the Bedrock Runtime and required telemetry endpoint security groups; a scoped S3 gateway endpoint permits only AgentCore service artifact access. Runtime inbound policies allow invocation only from the application role. MMDSv2 is required, processes run non-root, and session IDs are random per invocation because V1 is stateless.

## Principal-specific constraints

- **Application:** its broad private access is why it never receives an SES permission. It invokes the sender with an action ID, never a rendered body or recipient address. Against the compiler-owned view prefixes it holds `dynamodb:ConditionCheckItem` and nothing else, so it can refuse to commit an action proposal against a view that has moved without ever being able to move one itself.
- **Compiler:** accepts IDs and intent, then performs its own strongly consistent reads. It has no Bedrock permission, so policy cannot become probabilistic. Its only Core write is the short-lived send-authorization fence, and that is an IAM fact rather than a code convention: the fence has its own `NS#n#FENCE#k` partition ([ADR-019](../adr/ADR-019-send-fence-partition-isolation.md)), so `dynamodb:LeadingKeys` can scope the write to it. The case-version guard the compile transaction stages is `dynamodb:ConditionCheckItem` — read-only transactional authority — and case-partition writes are additionally denied outright. No `dynamodb:UpdateItem` is granted anywhere, and no blanket `dynamodb:TransactWriteItems` action is granted, because AWS authorizes a transaction through the permission each participant needs.
- **Action runtime:** has no tools registered in Strands. Network configuration permits only the Bedrock model path required by AgentCore; IAM remains the authoritative boundary.
- **Sender:** resolves the recipient from an allowlisted destination registry in configuration. It cannot read Core — that is a total explicit deny, not an absent grant — so even compromised rendering cannot fetch private details. It can invoke only the compiler's typed acquire/release fence operation and receives no private result. Its Shareable write is `PutItem` scoped by `LeadingKeys` to `NS#*#EXECUTION#*`, with writes to the action, action-current, view, view-current, and case prefixes denied by `ForAnyValue`. No `dynamodb:UpdateItem` is granted anywhere and no blanket `dynamodb:TransactWriteItems` is granted, because AWS authorizes a transaction through the permission each participant needs. Its SES allow is `ses:SendEmail` on the sending-identity and configuration-set ARNs and is deliberately **not** narrowed by `ses:Recipients` or `ses:FromAddress`, whose values are email addresses that would then live in a synthesized template; the single-recipient rule is enforced in code against the registry and a static assertion fails the build on any address-shaped string in the template.
- **Watcher:** accepts only `CommitmentDueEvent`, does not invoke an LLM, and cannot send external messages. The event is **not signed and not trusted**: it names which commitment to load, and namespace, case, generation, due event ID, due time, and status are all re-verified against the strongly-loaded row before the one compare-and-swap it may perform ([ADR-028](../adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 2–3).
- **Inbound mail:** authenticates the transport before it decodes anything, and holds no destination-address secret — the sender and recipient comparisons are digest comparisons against non-secret safe destination configuration ([ADR-026](../adr/ADR-026-inbound-reply-trust-and-correlation.md) § 3).
- **Audit readers:** a separate operational role may query audit records but is not part of the application runtime. Raw values are not stored in normal audit fields.

## AWS resource layout

One account and one primary Region per environment. Default demo Region is `us-east-1`; all resources are tagged `Project=ambient-chorus`, `Environment`, `Namespace`, and `DataClass`.

| Stack | Resources |
|---|---|
| `ChorusNetworkStack` | VPC, two isolated subnets, no NAT, AgentCore runtime security groups, Bedrock Runtime/CloudWatch Logs/X-Ray interface endpoints, scoped S3 gateway endpoint |
| `ChorusDataStack` | three DynamoDB tables, two S3 buckets, KMS aliases where customer-managed encryption is enabled, lifecycle policies |
| `ChorusAgentStack` | three AgentCore runtimes/endpoints, application inference profiles, distinct runtime roles/log groups |
| `ChorusComputeStack` | API Gateway HTTP API, FastAPI Lambda, application worker Lambda, compiler Lambda, sender Lambda, watcher Lambda, scheduler group/role, scheduler DLQ, SES configuration set |
| `ChorusWebStack` | private SPA bucket, CloudFront distribution, response/security headers |
| `ChorusObservabilityStack` | dashboards, alarms, log retention, X-Ray/OTEL wiring |

CDK v2 Python libraries are pinned through the `infra` uv dependency group; the matching CDK CLI is a pinned root npm dev dependency. AgentCore uses the stable `aws_cdk.aws_bedrockagentcore.Runtime`/CloudFormation resources, not the deprecated alpha module, with direct-code S3 artifacts and `networkMode=VPC`. CDK outputs endpoint ARNs and resource names; runtime application configuration receives them through Lambda environment variables.

## Environment behavior

| Environment | Storage/agents | External effects | Data lifecycle |
|---|---|---|---|
| `test` | in-memory repositories, fake clock/agents/storage/sender/scheduler | none | per test |
| `development` | DynamoDB Local; filesystem evidence/outbox; manual scheduler; fake agents by default, optional Bedrock | email to file only | developer-controlled `.local/` |
| `demo` | deployed AWS resources; live AgentCore/Bedrock/compiler/SES/scheduler | SES restricted to allowlisted verified destination | reset deletes only `DEMO` namespace |
| `production` | reserved; startup rejects it in V1 | blocked until identity/privacy review | no V1 retention behavior; a production ADR is required |

No implementation may treat `demo` as production-ready. A hard configuration validator rejects `ENVIRONMENT=production` in V1.

## Configuration contract

Configuration is loaded once into a strict Pydantic Settings model. Unknown `CHORUS_` variables fail startup; secrets never have defaults. AWS credentials use the default provider chain and are not environment fields in the app model.

```dotenv
# non-secret identity
CHORUS_ENVIRONMENT=development             # test|development|demo; production rejected in V1
CHORUS_NAMESPACE=LOCAL_ALICE                # test/development; deployed demo is exactly DEMO
CHORUS_AWS_REGION=us-east-1
CHORUS_LOG_LEVEL=INFO
CHORUS_POLICY_VERSION=policy/v1
CHORUS_PUBLIC_BASE_URL=http://localhost:5173

# persistence and storage
CHORUS_CORE_TABLE=chorus-core-development
CHORUS_SHAREABLE_TABLE=chorus-shareable-development
CHORUS_AUDIT_TABLE=chorus-audit-development
CHORUS_PRIVATE_EVIDENCE_BUCKET=chorus-private-evidence-development
CHORUS_EXPORT_EVIDENCE_BUCKET=chorus-export-evidence-development
CHORUS_DYNAMODB_ENDPOINT=http://localhost:8000  # development only; absent on AWS
CHORUS_LOCAL_DATA_DIR=.local

# agent endpoints and model resources
CHORUS_AGENT_MODE=fake                     # fake|agentcore; demo requires agentcore
CHORUS_MONITOR_RUNTIME_ARN=                 # required when mode=agentcore
CHORUS_INVESTIGATOR_RUNTIME_ARN=
CHORUS_ACTION_RUNTIME_ARN=
CHORUS_MONITOR_MODEL_PROFILE_ARN=
CHORUS_INVESTIGATOR_MODEL_PROFILE_ARN=
CHORUS_ACTION_MODEL_PROFILE_ARN=
CHORUS_AGENT_TIMEOUT_SECONDS=30

# action and scheduling
CHORUS_SENDER_FUNCTION_ARN=
CHORUS_COMPILER_FUNCTION_ARN=
CHORUS_WATCHER_FUNCTION_ARN=
CHORUS_WORKER_FUNCTION_ARN=
CHORUS_SCHEDULER_GROUP=chorus-development
CHORUS_SCHEDULER_ROLE_ARN=
CHORUS_SES_CONFIGURATION_SET=chorus-development
CHORUS_SES_FROM_IDENTITY_ID=chorus-demo-sender  # opaque stable sending-identity ID; never the address
CHORUS_DESTINATION_ID=property_manager:demo
CHORUS_DESTINATION_DISPLAY_LABEL=Property Management
CHORUS_DESTINATION_REGISTRY_VERSION=1
CHORUS_DESTINATION_ROUTING_TOKEN=00000000-0000-0000-0000-000000000000 # safe random UUID placeholder
CHORUS_DESTINATION_REGISTRY_SECRET_ARN=     # sender-only: same version/token, the verified destination address,
                                            # and the from_identity_id -> {from_address, reply_to_address,
                                            # identity_arn} entry. Binding the opaque identity ID in preview_hash
                                            # therefore binds the whole letterhead without naming a mailbox.
CHORUS_SES_IDENTITY_ARN=                    # sender-only IAM scope; deployment-supplied, never in a checked-in file
CHORUS_SEND_RECOVERY_WINDOW_SECONDS=60      # equals the fence's maximum life; a SENDING row is only reconcilable after it
CHORUS_DEMO_ACCESS_SECRET_ARN=              # deployed demo access token hash
CHORUS_DEMO_CLOCK_ENABLED=true              # rejected outside development/demo

# observability: no prompt/content capture
CHORUS_OTEL_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=
OTEL_SERVICE_NAME=chorus-api
```

The checked-in `.env.example` created in Phase 0 contains names and safe placeholders only. Local secrets go in ignored `.env`; deployed secrets go in Secrets Manager. `CHORUS_AGENT_MODE=fake`, local scheduler, file outbox, demo clock, `/demo/*`, and reset commands are rejected unless the environment allows them.

## Demo access model

Cognito is intentionally omitted. The deployed hackathon demo is a single-presenter environment protected by a high-entropy access token entered at runtime and stored only in browser `sessionStorage`; the API compares its hash to Secrets Manager. `X-Chorus-Demo-Actor` selects one of fixed demo personas only after token validation. This is not production authentication, is limited to the `DEMO` namespace, and is a recorded residual risk. API Gateway throttling, narrow CORS, and a reset-specific second confirmation string limit abuse.

## Deployment and rollback

1. CI validates Python/web/tests/import rules and `npm exec cdk -- --app "uv run python -m infra.cdk.app" synth`.
2. CDK deploys data, agents, compute, web, then observability. Runtime versions and endpoints are immutable; the application references an endpoint alias/version.
3. A post-deploy smoke test invokes each agent with non-sensitive fixture data, compiles an allow and a deny case, and uses an SES mailbox simulator or verified demo address.
4. Rollback changes the AgentCore endpoint/application alias and Lambda versions. Data schema changes must be backward-compatible within a release; destructive migrations are forbidden in V1.

CloudFormation deletion protection is enabled for non-disposable data stacks. Demo stack deletion retains buckets/tables by default; `uv run chorus-demo reset --namespace DEMO --confirm DEMO` is the only routine data cleanup path.
