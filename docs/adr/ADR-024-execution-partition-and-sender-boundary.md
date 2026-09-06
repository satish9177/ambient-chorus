# ADR-024: Executions get their own Shareable partition, and `W(execution only)` becomes true

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § IAM notation and resources, § Principal-specific constraints, § Configuration contract; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping, § Access patterns; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Action pipeline, § Destination and SES controls; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register (T25); [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) § IAM boundary tests

## Context

### The same sentence, one table over

[ADR-019](ADR-019-send-fence-partition-isolation.md) found that this row of the frozen trust matrix could not be enforced:

```text
Compiler Lambda | Core: R(all) / W(fence only)
```

because `dynamodb:LeadingKeys` filters partition keys and DynamoDB exposes no condition key over sort keys, so the narrowest possible grant reached every item in every case partition. Phase 6 discovered it by synthesizing the role.

Phase 8 synthesizes the sender role, and the matrix says:

```text
Sender Lambda | Share: R(view/proposal/approval) / W(execution only)
```

The Shareable key grammar is:

```text
PK = NS#{namespace}#ACTION#{action_id}
  SK ACTION               the immutable ActionProposal
  SK APPROVAL#{approval_id}   the immutable human decision
  SK EXECUTION#{execution_id} the guarded send state
```

The execution shares its partition with the two immutable artifacts the send exists to honour. A grant of `dynamodb:PutItem` over `NS#*#ACTION#*` — the narrowest grant that can write an execution — authorizes overwriting the **proposal** and the **approval** in the same partition.

The effective answer to "can sender credentials rewrite the message a human approved" is **yes**, against a document that says the sender's only Shareable write is the execution.

### Why this one is worse than the fence

T25 names a compromised sender as in scope, and its stated bound is "one fixed recipient; one deterministic template; no arbitrary URLs/attachments". Every one of those bounds is a property of artifacts that live in the partition the sender would be able to write.

A compromised sender that can put `SK=ACTION` can replace the proposal's `subject`, claims, `requested_action`, and — because it also writes `preview_hash` and `proposal_hash` — can make the replacement self-consistent. It then renders the new proposal, recomputes a matching `rendered_message_hash`, and the equality check that exists to prove "the sender sent what the human approved" passes. The deterministic renderer, the citation grammar, the human preview, and the approval digest chain would all be intact and all be describing a message nobody approved.

That is the entire Phase-8 guarantee, defeated by a key layout.

### The same defect reaches the idempotency records

`IdempotencyPartitionKind.ACTION` places a record at `NS#n#ACTION#a`. Every Phase-8 send record the sender must write — its claim proof and its result proof — would therefore land in the partition the sender must not be able to write. This is the mirror image of the reason [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) already gives for putting the compile's record under `VIEW_CURRENT`: a record that only one principal must write must not sit where only that principal is denied.

## Decision

**Move `ActionExecution` to its own Shareable partition.**

| | Partition key | Sort key |
|---|---|---|
| Old | `NS#{namespace}#ACTION#{action_id}` | `EXECUTION#{execution_id}` |
| **New** | `NS#{namespace}#EXECUTION#{action_id}` | `EXECUTION#{execution_id}` |

The execution **stays in the Shareable table**. Only its partition placement changes. No new table is introduced. The partition is keyed by `action_id` rather than `execution_id` so it remains the per-action collection the access patterns describe; V1 puts exactly one item in it.

The send-command idempotency records move with it. `IdempotencyPartitionKind` gains `EXECUTION`, whose partition is `NS#{namespace}#EXECUTION#{action_id}`, joining `VIEW_CURRENT` as the second entry whose placement is a permission fact rather than a filing choice.

### 1. What is unchanged

- **Execution semantics.** The state machine, the presence table, monotonic presence, the one-attempt rule, and every compare-and-swap are untouched.
- **Ownership of the states.** The application writes `DRAFT`, `DRAFT→APPROVED`, and the two human-caused `→FAILED` edges. The sender writes `APPROVED→SENDING` and the three terminal outcomes. The split is enforced by the state machine's conditions and by which principal runs which command, not by IAM — and that is stated rather than implied, because IAM cannot separate two writers of one item type.
- **No lookup is lost.** `CurrentActionPointer` already carries `execution_id` — "named rather than derived", precisely so a strongly read pointer answers "which execution belongs to the current proposal" without a lookup. Every execution read is a direct get from `{action_id, execution_id}`, both of which the pointer holds. No query changes and no GSI appears.
- **Frozen bounds.** `MAX_ACTIONS_PER_CASE = 10` still bounds the total, and each new partition holds one item.

### 2. Migration

**None.** V1 has not been deployed, so no stored execution exists at the old address.

`chorus-demo reset` — Phase-11 work — **must include the `NS#{namespace}#EXECUTION#{action_id}` roots** in its manifest alongside the action partitions it already clears, exactly as [ADR-019](ADR-019-send-fence-partition-isolation.md) required for the fence roots. A reset that cleared an action and left its execution would leave a `SENT` or `SEND_UNKNOWN` row addressable under an action that no longer exists, and [08-api-design.md](../architecture/08-api-design.md) makes reset refuse while such a row is live.

### 3. The sender's complete boundary

Every grant, and every deny, in one place. `dynamodb:UpdateItem` appears nowhere: every execution write is a conditional `PutItem` of the whole record. The blanket `dynamodb:TransactWriteItems` action is not granted, because AWS authorizes a transaction through the permission each participant needs.

| Grant | Actions | Resource / scope |
|---|---|---|
| Read Shareable | `GetItem`, `Query` | Shareable table, unrestricted — it must load the view, the proposal, the approval, and both pointers |
| Write executions | `PutItem`, `ConditionCheckItem` | Shareable table, `ForAllValues:StringLike` `dynamodb:LeadingKeys` = `NS#*#EXECUTION#*` |
| Append audit | `PutItem` | Audit table |
| Acquire and release the fence | `lambda:InvokeFunction` | the compiler function ARN only |
| Resolve the destination | `secretsmanager:GetSecretValue` | the destination-registry secret ARN only |
| Decrypt that secret | `kms:Decrypt` | that secret's key only |
| Send | `ses:SendEmail` | the sending-identity ARN and the configuration-set ARN, both from deployment configuration |
| Own logs | `CreateLogStream`, `PutLogEvents`, `DescribeLogStreams` | its own log group only |

| Deny | Actions | Scope |
|---|---|---|
| Shareable writes outside executions | `PutItem`, `UpdateItem`, `DeleteItem` | `ForAnyValue:StringLike` `LeadingKeys` = `NS#*#ACTION#*`, `NS#*#ACTION_CURRENT#*`, `NS#*#VIEW#*`, `NS#*#VIEW_CURRENT#*`, `NS#*#CASE#*` |
| All Core access | `dynamodb:*` | the Core table ARN |
| Private and export objects | `s3:*` | both bucket ARNs and their objects |
| Model access | `bedrock:*`, `bedrock-agentcore:*` | `*` |
| Scheduler | `scheduler:*` | `*` |

`ForAnyValue` on the write deny is deliberate and is the same choice [ADR-022](ADR-022-action-draft-preview-and-transaction.md) made for the application's view deny: a transaction naming *any* proposal- or approval-partition item alongside legitimate execution items is refused whole, rather than permitted because most of its keys were acceptable.

The Core deny is total rather than absent. The sender resolves its recipient from an allowlisted registry in configuration; it has no reason to read a case, a fact, a mandate, or the fence row, and it acquires the fence by invoking the compiler's typed operation rather than by touching the item.

**That deny has to be satisfiable by the code, and the Phase-8 repair pass found that it was not.** The sender's composition root built a `CoreRepository` and an in-process `SendAuthorization` for the deployed composition as well as the local one — an object graph that works against local storage and returns `AccessDenied` in an account, so the synthesized component could not perform its own send-time authorization at all. Nothing caught it because no test runs without Core.

The boundary is now a port. `chorus.ports.send_authorization.SendAuthorizationPort` has two methods — acquire-with-revalidation, and release — and two implementations: the in-process one for `test` and `development`, where there is no Lambda boundary to cross, and `CompilerSendAuthorization` for a deployed sender, which holds the `lambda:InvokeFunction` grant above and **no repository, table name, or storage driver of any kind**. A static test walks the constructed object graph and fails if a deployed composition reaches a Core handle anywhere in it, which is the assertion that makes this row of the trust matrix true rather than aspirational.

### 4. The application also writes executions, and that is not a widening

`APPLICATION_SHAREABLE_PREFIXES` gains `NS#*#EXECUTION#*`. The application already creates the `DRAFT` execution as participant 2 of the proposal apply and moves it on both human decisions, so this is where a write it already had now lives.

Two principals therefore hold `PutItem` over one prefix. What keeps them apart is the state machine: `ACTION_EXECUTION_EDGES` plus the `expected_version` condition means the application can only move a row that is in `DRAFT` or `APPROVED`, and the sender can only move one that is in `APPROVED` or `SENDING`. The one overlapping state, `APPROVED`, is the withdrawal race [ADR-023](ADR-023-approval-binding-and-immutability.md) § 8 resolves with exactly one winner.

**This is written down rather than left implied**, because it is the one boundary in Phase 8 that IAM does not draw. An assertion that it holds is a test over the transitions, not over a policy.

### 5. The sender's SES grant, and why no address appears in a template

The allow is `ses:SendEmail`. SES v2's `SendEmail` API authorizes under that same `ses:` action name; the `sesv2:` entries in the existing deny lists name no real IAM action and are inert, which is harmless in a deny and would be a silent hole in an allow. They are left in place as defence in depth and the allow is written correctly.

The grant is scoped to two ARNs from deployment configuration: the verified sending identity and the `chorus-{environment}` configuration set. It is **not** further scoped with `ses:Recipients` or `ses:FromAddress`, and that omission is a decision rather than an oversight: those condition values are email addresses, and a synthesized CloudFormation template is a build artifact that gets read, diffed, and attached to a pull request. Putting the demo destination address into it would move a value that lives in Secrets Manager into version control.

The single-recipient rule is therefore enforced where it can be enforced without publishing an address:

- the sender asserts exactly one `ToAddresses` entry and no `Cc` or `Bcc`, refusing any other count before the call ([ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 6);
- the destination registry resolves `{destination_id, registry_version, routing_token}` — all three of which are inside `preview_hash` — to exactly one address, and any mismatch denies;
- a static assertion scans the whole synthesized template for an address-shaped string and fails if one appears.

### 6. What follows for the compiler

Nothing. The fence stays at `NS#{namespace}#FENCE#{case_id}` / `SEND_FENCE` in Core, the compiler remains its sole writer, and the sender reaches it only through `AcquireSendAuthorizationFence` and `ReleaseSendAuthorizationFence`. The compiler's Shareable read already covers the new partition, because its Shareable read is unrestricted over safe records.

## Alternatives considered

- **Grant the sender `PutItem` on `NS#*#ACTION#*` and rely on repository code to write only executions.** Rejected: it is the exact "document the broad write as accepted residual risk" that [ADR-019](ADR-019-send-fence-partition-isolation.md) refused, and here the items in reach are the proposal and the approval — the two artifacts the entire approval chain is anchored to.
- **Have the application worker perform the `APPROVED→SENDING` claim, leaving the sender with no Shareable write at all.** Genuinely tempting, and rejected because it moves the failure rather than removing it. The claim and the SES call would then sit on either side of a Lambda invocation, so a lost invoke would strand an execution in `SENDING` that SES had definitely never seen — quarantining a case to `SEND_UNKNOWN` for a network blip. Keeping the claim in the sending process makes that window in-process, and a lost dispatch leaves the execution safely at `APPROVED` where a retry is correct ([ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 3).
- **A separate executions table.** Rejected for the reason [ADR-019](ADR-019-send-fence-partition-isolation.md) rejected a fence table: a table is a bigger unit than the problem, and the trust split the three tables express is private / shareable / audit. An execution is shareable state.
- **Restrict the write with `dynamodb:Attributes`.** Rejected as insufficient, identically to [ADR-019](ADR-019-send-fence-partition-isolation.md): it filters attribute names rather than values, so a proposal row could still be overwritten with an execution-shaped item.
- **Key the new partition by `execution_id`.** Rejected as slightly worse: it discards the per-action collection for no gain, since the pointer already supplies both identifiers and V1 has one execution per action either way.
- **Deny-only, leaving the execution in the action partition.** Rejected as the sole fix, for the reason already recorded: relying on a deny to carve a hole out of an over-broad allow means the allow still describes the wrong boundary.
- **Scope the SES grant with `ses:Recipients`.** Rejected: it would publish the demo destination address in a synthesized template, and the same constraint is enforceable in code against a registry the sender already has to read.

## Why chosen

It is the same fix as [ADR-019](ADR-019-send-fence-partition-isolation.md), applied to the second place the same constraint bites, found the same way — by synthesizing a role and checking whether the sentence beside it was true. The pattern is now explicit rather than incidental: **any Shareable or Core item whose writer must be separable from the rest of its partition is keyed by its own entity-type prefix, and that is decided when the item is designed.**

It also restores what T25 claims. With the proposal and the approval out of reach, a compromised sender can send the message a human approved, or send nothing; it cannot compose a different one and make every hash agree.

## Consequences

- `chorus.infrastructure.dynamodb.keys` gains `execution_partition(namespace, action_id)`, and `codec_share` resolves every execution key through it — load, create, update, and condition together or not at all.
- `IdempotencyPartitionKind` gains `EXECUTION`, requiring `action_id`, and its partition builder joins the existing set. `IdempotencyPartition.__post_init__`'s required-field table gains the row.
- [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping gains an `Execution` row at `NS#n#EXECUTION#a` and drops it from the action partition; § Access patterns records that the action-partition query now retrieves proposal and approval lineage, and the execution is a direct get from the pointer's `execution_id`.
- [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md)'s trust matrix sender row becomes `R(all safe) / W(`EXECUTION` partition only)`, and the application row's Shareable write prefixes gain `EXECUTION#`.
- `infra/cdk/stacks/sender.py` is introduced by Phase 8 with the role, log group, and SES configuration set **defined and asserted but not deployed** — the same static-now, live-in-Phase-11 split Phase 6 used for the compiler and Phase 7 for the Action runtime.
- Static CDK tests assert the **negative** capability by exhaustive search over the synthesized statements, in the manner [ADR-019](ADR-019-send-fence-partition-isolation.md) established: no allow granted to the sender may name a write action against leading keys outside `NS#*#EXECUTION#*`, no allow may reach the Core table, and no statement anywhere in the template may contain an address-shaped string.
- [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md)'s post-deploy sender canary gains a proposal-write probe: an attempt to put or delete an item in an `ACTION#` partition under the sender role must return `AccessDenied`.
- Threat register **T25** gains the partition split as its stated mitigation; **T33** is added for the specific escalation this ADR closes — a compromised sender rewriting the immutable proposal and sending self-consistent unapproved content.
- Named tests gain `test_sender_cannot_write_any_action_or_view_prefix` and `test_synthesized_template_contains_no_address_shaped_string`.

## Revisit condition

Revisit if DynamoDB ever gains a condition key that constrains sort keys, which would make entity-type partition prefixes an optimisation rather than a security requirement — the same condition [ADR-019](ADR-019-send-fence-partition-isolation.md) records.

Revisit § 4's two-writer arrangement if a third principal is ever proposed as a writer of executions. Two writers separated by a state machine is a boundary a test can prove; three would be a claim about who calls what.
