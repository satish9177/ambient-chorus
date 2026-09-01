"""Conversion between the restricted stored value domain and DynamoDB attribute values.

Only ``S``, ``N``, ``BOOL``, ``NULL``, ``L``, and ``M`` are produced or accepted. Binary, set,
and floating-point attribute types have no representation here, so a stored authorization
artifact cannot contain an unrepresentable or lossy value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from chorus.domain.errors import IntegrityError
from chorus.ports.storage import StoredItem, StoredValue

type AttributeValue = dict[str, "str | bool | list[AttributeValue] | dict[str, AttributeValue]"]
type AttributeMap = dict[str, AttributeValue]

CANONICAL_NUMBER: Final = re.compile(r"0|-?[1-9][0-9]*")
"""The only numeric spelling this codec writes, and therefore the only one it reads.

``encode_value`` emits ``str(int)`` and nothing else, and DynamoDB returns those values
unchanged. Python's ``int`` is far more permissive than that -- it accepts a leading ``+``,
surrounding whitespace, leading zeros, digit-group underscores, and non-ASCII digits -- so two
different stored spellings could otherwise decode to one authorization value such as a
version, a fence deadline, or a contributor count. Only the exact form this codec could have
produced is accepted, which is why ``-0`` is rejected alongside ``+0``: ``str(int)`` writes
zero one way.
"""


def encode_value(value: StoredValue) -> AttributeValue:
    """Encode one restricted stored value."""

    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, tuple):
        return {"L": [encode_value(item) for item in value]}
    if isinstance(value, Mapping):
        return {"M": {key: encode_value(item) for key, item in value.items()}}
    raise IntegrityError("ATTRIBUTE:unsupported_type")


def encode_item(item: StoredItem) -> AttributeMap:
    return {key: encode_value(value) for key, value in item.items()}


def decode_value(value: AttributeValue) -> StoredValue:
    """Decode one attribute value, failing closed on an unknown or ambiguous tag."""

    if len(value) != 1:
        raise IntegrityError("ATTRIBUTE:ambiguous")
    tag, raw = next(iter(value.items()))
    if tag == "NULL":
        if raw is not True:
            raise IntegrityError("ATTRIBUTE:null")
        return None
    if tag == "BOOL":
        if not isinstance(raw, bool):
            raise IntegrityError("ATTRIBUTE:bool")
        return raw
    if tag == "S":
        if not isinstance(raw, str):
            raise IntegrityError("ATTRIBUTE:string")
        return raw
    if tag == "N":
        if not isinstance(raw, str) or CANONICAL_NUMBER.fullmatch(raw) is None:
            raise IntegrityError("ATTRIBUTE:number")
        return int(raw)
    if tag == "L":
        if not isinstance(raw, list):
            raise IntegrityError("ATTRIBUTE:list")
        return tuple(decode_value(item) for item in raw)
    if tag == "M":
        if not isinstance(raw, dict):
            raise IntegrityError("ATTRIBUTE:map")
        return {key: decode_value(item) for key, item in raw.items()}
    raise IntegrityError("ATTRIBUTE:unsupported_tag")


def decode_item(item: AttributeMap) -> StoredItem:
    return {key: decode_value(value) for key, value in item.items()}
