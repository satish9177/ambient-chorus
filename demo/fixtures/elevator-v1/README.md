# Elevator v1 synthetic ambient corpus

This directory is the frozen Phase 3 input for the demo. It is **input data**, not an answer
key: it contains messages, actors, timestamps, and attachment provenance, and it deliberately
contains no report identifier, no fact identifier, no case identifier, and no statement about
which messages belong together. Discovery happens at runtime through the Monitor contract.

## Contents

| File | Purpose |
|---|---|
| `manifest.json` | Seed version, community, pseudonymous actor registry, evidence catalog, and the corpus digest |
| `feed.json` | The 24 fixed ambient messages in channel order |
| `evidence/elevator-e42.jpg` | The lift control-panel photograph attached to message 16 |
| `evidence/injection-notice.txt` | The malicious document attached to message 18 |
| `evidence/management-reply.eml` | The manager reply, staged in the catalog and **not** ingested until the Phase 9 external-reply step |

`SyntheticAmbientAdapter` verifies every declared checksum and byte length before returning a
single message. An edited corpus, a changed evidence file, or an unknown manifest field fails
closed with an integrity error rather than being replayed as the frozen fixture.

## What the corpus is designed to contain

Six elevator incidents from four residents spread over six days, plus private identity, health,
and unit details from one resident, a contradicting management statement, one photograph, one
prompt-injection message, and thirteen unrelated messages about parcels, parking, plumbing,
bins, laundry, water pressure, a dog, and a notice board.

Several incidents deliberately never name the equipment -- "we were stuck between the third and
fourth floor", "it stalled at the second floor" -- so that a keyword rule cannot find the
pattern and a model has to read for meaning.

## Identifiers

Fixture identifiers are `uuid5` of `ambient-chorus/elevator-v1/{name}` under the namespace
recorded in the manifest. Durable identity for anything discovered at runtime is derived from
validated inputs instead, never from a fixture name.

The Phase 1 domain-level fixture builder remains `tests/fixtures/elevator.py`.
