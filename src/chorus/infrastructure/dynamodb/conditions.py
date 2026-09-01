"""Rendering of typed conditions into DynamoDB condition expressions.

Callers never write an expression string. Each closed condition variant renders to a fixed
fragment with generated placeholders, so a stored attribute name can never be interpolated
into an expression and every attribute name is escaped against DynamoDB's reserved words.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chorus.infrastructure.dynamodb.attributes import AttributeMap, encode_value
from chorus.infrastructure.dynamodb.codec import ATTR_PARTITION_KEY, ATTR_SORT_KEY
from chorus.ports.storage import (
    AllOf,
    AnyOf,
    AttributeAtMostNumber,
    AttributeEqualsNumber,
    AttributeEqualsString,
    ItemCondition,
    KeyAbsent,
    KeyPresent,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderedCondition:
    """A condition expression plus the placeholder maps it needs."""

    expression: str
    names: dict[str, str] | None = None
    values: AttributeMap | None = None


@dataclass(slots=True)
class _Renderer:
    names: dict[str, str] = field(default_factory=dict)
    values: dict[str, object] = field(default_factory=dict)
    counter: int = 0

    def name(self, attribute: str) -> str:
        for placeholder, existing in self.names.items():
            if existing == attribute:
                return placeholder
        self.counter += 1
        placeholder = f"#a{self.counter}"
        self.names[placeholder] = attribute
        return placeholder

    def value(self, raw: str | int) -> str:
        self.counter += 1
        placeholder = f":v{self.counter}"
        self.values[placeholder] = raw
        return placeholder

    def render(self, condition: ItemCondition) -> str:
        match condition:
            case KeyAbsent():
                return (
                    f"attribute_not_exists({self.name(ATTR_PARTITION_KEY)})"
                    f" AND attribute_not_exists({self.name(ATTR_SORT_KEY)})"
                )
            case KeyPresent():
                return (
                    f"attribute_exists({self.name(ATTR_PARTITION_KEY)})"
                    f" AND attribute_exists({self.name(ATTR_SORT_KEY)})"
                )
            case AttributeEqualsString(name=name, value=value):
                return f"{self.name(name)} = {self.value(value)}"
            case AttributeEqualsNumber(name=name, value=value):
                return f"{self.name(name)} = {self.value(value)}"
            case AttributeAtMostNumber(name=name, value=value):
                return f"{self.name(name)} <= {self.value(value)}"
            case AllOf(conditions):
                return " AND ".join(f"({self.render(inner)})" for inner in conditions)
            case AnyOf(conditions):
                return " OR ".join(f"({self.render(inner)})" for inner in conditions)
            case _:  # pragma: no cover - the condition union is closed
                raise AssertionError("unreachable item condition")


def render_condition(condition: ItemCondition) -> RenderedCondition:
    """Render one closed condition into a DynamoDB expression."""

    renderer = _Renderer()
    expression = renderer.render(condition)
    encoded_values: AttributeMap = {}
    for placeholder, raw in renderer.values.items():
        if isinstance(raw, bool) or not isinstance(raw, int | str):  # pragma: no cover
            raise AssertionError("unsupported condition value")
        encoded_values[placeholder] = encode_value(raw)
    return RenderedCondition(
        expression=expression,
        names=dict(renderer.names) if renderer.names else None,
        values=encoded_values or None,
    )
