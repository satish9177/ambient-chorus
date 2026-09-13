"""Condition-only Core authority for the normal DEMO reset interlock (ADR-031)."""

from aws_cdk import aws_iam as iam


def grant_demo_reset_condition(
    role: iam.Role, core_arn: str, *, deny_other_conditions: bool = False
) -> None:
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ConditionCheckDemoResetLock",
            effect=iam.Effect.ALLOW,
            actions=["dynamodb:ConditionCheckItem"],
            resources=[core_arn],
            conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["NS#DEMO"]}},
        )
    )
    if deny_other_conditions:
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyOtherCoreConditions",
                effect=iam.Effect.DENY,
                actions=["dynamodb:ConditionCheckItem"],
                resources=[core_arn, f"{core_arn}/*"],
                conditions={"ForAnyValue:StringNotEquals": {"dynamodb:LeadingKeys": ["NS#DEMO"]}},
            )
        )
