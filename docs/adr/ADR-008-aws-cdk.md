# ADR-008: AWS CDK v2 in Python for infrastructure

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

V1 needs AgentCore Runtime, Lambda/API Gateway, DynamoDB, S3/KMS, SES, EventBridge Scheduler/DLQ, CloudFront, IAM, logs, and testable policy assertions. The team is already using Python.

## Decision

Use AWS CDK v2 in Python, including stable `aws_cdk.aws_bedrockagentcore` constructs/CloudFormation resources. Split data, agents, compute, web, and observability stacks. Pin `aws-cdk-lib`/construct libraries through `uv` and the matching CDK CLI through the root npm lock; run synth and assertions in CI.

## Alternatives considered

- Terraform: mature and portable, but adds HCL/provider workflow and no portability requirement.
- SAM: excellent for Lambda/API but less cohesive for AgentCore/web/IAM/resource policies.
- CloudFormation YAML: direct but verbose and harder to unit-test/refactor safely.
- shell/console setup: quick initially, unreproducible and weak for IAM review.

## Why chosen

CDK provides one language/toolchain, current AgentCore resources, reusable least-privilege constructs, dependency wiring, and synthesized-template assertions.

## Consequences

- CDK/bootstrap/tool versions must be pinned and synthesized output reviewed.
- Avoid abstractions that hide IAM; security-sensitive policies stay explicit.
- Deployment still needs Bedrock/SES account prerequisites.

## Revisit condition

Revisit if organizational infrastructure mandates Terraform/SAM or CDK lacks a required stable resource. Migration must preserve policy assertions and deployment order.
