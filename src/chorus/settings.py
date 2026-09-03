"""Strict Phase 0 configuration loading."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chorus.ports.retention import AuditRetention


class Environment(StrEnum):
    """Supported V1 environments."""

    TEST = "test"
    DEVELOPMENT = "development"
    DEMO = "demo"


class AgentMode(StrEnum):
    """Agent adapter selected by configuration."""

    FAKE = "fake"
    AGENTCORE = "agentcore"


class Settings(BaseSettings):
    """Validated process settings loaded once at a composition root."""

    model_config = SettingsConfigDict(
        env_prefix="CHORUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        case_sensitive=False,
    )

    _PREFIX: ClassVar[str] = "CHORUS_"

    environment: Environment = Environment.DEVELOPMENT
    namespace: str = Field(default="LOCAL_DEVELOPER", pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    aws_region: str = Field(default="us-east-1", min_length=1, max_length=32)
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    policy_version: str = Field(default="policy/v1", pattern=r"^policy/v1$")
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:5173")

    core_table: str = "chorus-core-development"
    shareable_table: str = "chorus-shareable-development"
    audit_table: str = "chorus-audit-development"
    private_evidence_bucket: str = "chorus-private-evidence-development"
    export_evidence_bucket: str = "chorus-export-evidence-development"
    dynamodb_endpoint: AnyHttpUrl | None = AnyHttpUrl("http://localhost:8000")
    local_data_dir: Path = Path(".local")

    agent_mode: AgentMode = AgentMode.FAKE
    monitor_runtime_arn: str | None = None
    investigator_runtime_arn: str | None = None
    action_runtime_arn: str | None = None
    monitor_model_profile_arn: str | None = None
    investigator_model_profile_arn: str | None = None
    action_model_profile_arn: str | None = None
    agent_timeout_seconds: int = Field(default=90, ge=1, le=300)
    """The outermost rung of the agent timeout hierarchy.

    It bounds how long the application waits on one AgentCore invocation, and it must exceed
    the runtime's own budget, which must exceed the model read timeout. The ordering is the
    invariant, not the numbers: if the application gave up first, it would launch its one
    licensed retry while the first runtime was still running, and two runtimes would be
    reading the same private batch at once. A test asserts the ordering against the values the
    runtime artifact actually declares.
    """

    sender_function_arn: str | None = None
    compiler_function_arn: str | None = None
    watcher_function_arn: str | None = None
    worker_function_arn: str | None = None
    scheduler_group: str = "chorus-development"
    scheduler_role_arn: str | None = None
    ses_configuration_set: str = "chorus-development"
    destination_id: str = "property_manager:demo"
    destination_display_label: str = "Property Management"
    destination_registry_version: int = Field(default=1, ge=1)
    destination_routing_token: UUID = UUID("00000000-0000-0000-0000-000000000000")
    destination_registry_secret_arn: str | None = None
    demo_access_secret_arn: str | None = None
    demo_clock_enabled: bool = True
    otel_enabled: bool = False

    @model_validator(mode="after")
    def validate_environment_contract(self) -> Settings:
        """Reject environment combinations that would create an unsafe fallback."""

        if self.environment is Environment.DEMO:
            if self.namespace != "DEMO":
                raise ValueError("demo environment requires the DEMO namespace")
            if self.agent_mode is not AgentMode.AGENTCORE:
                raise ValueError("demo environment requires agentcore mode")
        if self.agent_mode is AgentMode.AGENTCORE:
            required = (
                self.monitor_runtime_arn,
                self.investigator_runtime_arn,
                self.action_runtime_arn,
                self.monitor_model_profile_arn,
                self.investigator_model_profile_arn,
                self.action_model_profile_arn,
            )
            if any(value is None or value == "" for value in required):
                raise ValueError("agentcore mode requires all runtime and model profile ARNs")
        return self

    @classmethod
    def load(cls) -> Settings:
        """Load settings and fail if the process contains an unknown CHORUS variable."""

        known = {f"{cls._PREFIX}{name.upper()}" for name in cls.model_fields}
        unknown = sorted(
            key for key in os.environ if key.startswith(cls._PREFIX) and key not in known
        )
        if unknown:
            raise ValueError(f"unknown CHORUS configuration variable(s): {', '.join(unknown)}")
        return cls()


def audit_retention_for(environment: Environment) -> AuditRetention:
    """Map a deployment environment onto its frozen audit retention.

    This mapping lives with configuration rather than with the repository because how long a
    *deployment* keeps its audit trail is a property of the deployment, not of the storage
    adapter. ``AuditRepository`` is handed the resulting policy and never learns which
    environment produced it.

    Only the demo deployment expires audit events, after 90 days. Everywhere else the trail is
    kept until an operator removes it, because an expiring audit row in a durable environment
    would quietly erase the record of a security decision. The table keeps its TTL attribute
    configured in every environment regardless; what changes is whether an item carries one.
    """

    if environment is Environment.DEMO:
        return AuditRetention.demo()
    return AuditRetention.durable()
