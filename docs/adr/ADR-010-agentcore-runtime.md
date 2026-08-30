# ADR-010: Separate AgentCore Runtime deployment for all three agents

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

The preferred stack includes Strands/Bedrock and asks whether AgentCore Runtime is stable/beneficial. V1 needs distinct runtime identities, isolated artifacts, IAM-authenticated invocation, versioned endpoints, and useful agent tracing. Agent state does not need persistence.

## Decision

Deploy Monitor, Investigator, and Action as three distinct Bedrock AgentCore Runtime direct-code Python 3.12 resources/endpoints with IAM inbound authorization, dedicated roles/log groups/application inference profiles, MMDSv2 required, non-root execution, stateless random sessions, no persistent filesystem, and no AgentCore Memory/Gateway/tools. Put all runtimes in two isolated VPC subnets with no NAT/internet; allow only endpoint-scoped Bedrock Runtime, telemetry, and service artifact traffic. All use Nova 2 Lite through Strands structured output. Action receives the strictest explicit denies.

Current AWS documentation confirms AgentCore Runtime supports custom/Strands agents, isolated microVM sessions, IAM roles, immutable runtime versions/endpoints, stable CloudFormation/CDK resources, and VPC mode without internet unless a NAT route is deliberately added: [Runtime overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html), [security practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html), [VPC configuration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html), and [CDK Runtime](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrockagentcore/Runtime.html).

## Alternatives considered

- All agents inside API/worker Lambda: fewer resources, but shared credentials/artifacts weaken trust proof and agent observability.
- AgentCore only for Action: good minimum boundary but produces two deployment/invocation models and weaker Monitor/Investigator isolation.
- Bedrock Agents managed orchestration: conflicts with explicit Strands/application orchestration.
- AgentCore Graph/harness/Memory: unnecessary implicit state/orchestration/capability.

## Why chosen

Separate Runtime principals/artifacts make trust zones and version rollback concrete with modest V1 complexity now that stable CDK resources exist. A uniform adapter simplifies operations while IAM remains different per role.

## Consequences

- Three runtime deployments and cold-start/service dependency must be tested before demo.
- AgentCore is not a data store; every invocation carries bounded state.
- Local development uses fake agents by default; deployed smoke/evaluation is mandatory.
- Model/runtime upgrades require evaluation; model IDs are not silently changed via environment.

## Revisit condition

Fall back to Lambda for Monitor/Investigator only if measured AgentCore availability/latency/deployment complexity threatens the demo and Action isolation remains intact. Record the exception in a superseding ADR. Revisit model choice before Nova 2 Lite lifecycle changes or when evaluation misses quality targets.
