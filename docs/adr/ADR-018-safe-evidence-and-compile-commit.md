# ADR-018: The safe-evidence derivative, and one commit point for a compile

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § ShareableEvidenceRef, § CommunityCase; [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) § Minimum-necessary transformation rules, § Canonical serialization and hashes; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Audit table mapping, § Export evidence bucket, § Transaction boundaries, § Safe photo derivative; [08-api-design.md](../architecture/08-api-design.md) § Compile view; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register (T28); [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) § Persistence and AWS adapter tests

## Context

The Phase-6 pre-implementation review found four things the frozen documents named but never defined, and each of them is a place where an implementer would otherwise have had to invent security-sensitive semantics while writing code.

### The commit point was ambiguous

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries said safe derivatives are "first written with `pending-compile-id`", then a cross-table transaction writes the view. The same document's § Export evidence bucket fixed the object key as

```text
ns/{namespace}/community/{community_id}/case/{case_id}/view/{view_id}/evidence/{safe_evidence_ref_id}/content
```

Those cannot both hold. `view_id` and `safe_evidence_ref_id` are minted *during* the compile, so an object written before the transaction cannot already sit at that key; and if the object reaches its final key by a copy *after* the transaction, then a copy failure leaves a committed, current, immutable `ShareableCaseView` referring to an object that does not exist. [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) covered "S3 derivative written but DB compile fails" and did not cover that one, because under the frozen prose it was not supposed to be reachable.

"Where is the commit point" is the first question a reviewer asks about a two-store write, and the documents answered it twice.

### The image pipeline was named, not specified

§ Safe photo derivative required "a deterministic image library" that "decodes and re-encodes to PNG, strips EXIF/comments/profiles, caps dimensions, and recomputes SHA-256", and required a decompression bomb, an unexpected frame, and a decode failure to fail closed. No library was chosen, no cap had a number, and no encoder setting was fixed — while [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) already required a golden hash over the output. Determinism of PNG bytes is a property of a specific encoder and its zlib, not of the format, so a golden hash without a pinned encoder is a test that passes until someone upgrades something.

The frozen risk register carries this as R16, and the Phase-6 entry of [implementation-plan.md](../plans/implementation-plan.md) names "image library/parser vulnerabilities" as the phase's headline risk. A hardening profile that exists only in an implementer's head is not a mitigation.

### The review was a boolean with four names

§ Safe photo derivative required a review recording `NO_FACE`, `NO_UNIT`, `NO_NAME`, `NO_HEALTH` and a safe caption, and stated that "review is an input artifact, not an LLM judgment". `chorus.privacy.policy.SafeEvidenceCandidate` models the whole of it as `human_reviewed: bool`, and the elevator manifest carries only `safe_caption`. Nothing said who may mark evidence safe, where the record lives, or what proof is persisted — so "fixed review" was readable as fixture metadata, as a stored human decision, or as a service somebody would later feel entitled to add.

### Nothing recorded which source fact became which exported fact

[04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) removes source lineage from `ShareableFact` and `ShareableEvidenceRef` and says it "is stored in the private compiler audit projection". That projection had no schema, no key, no table row, and no codec. `AuditEvent.safe_details` is the closed shape `{count, rule_id}`, which has nowhere to put a scope or a per-fact reason code, and one audit event per fact is not available either: `MAX_REQUESTED_FACTS` and `MAX_ACTIVE_FACTS_PER_CASE` are both `100`, so a hundred per-fact events plus the compile's fixed participants exceeds `TRANSACTION_MAX_OPERATIONS = 100`. The bound decides the shape.

## Decision

### 1. The export derivative is content-addressed and written exactly once

The sanitized derivative is written **before** the DynamoDB transaction, directly and only to:

```text
ns/{namespace}/community/{community_id}/case/{case_id}/evidence/{derivative_sha256}/content
```

`derivative_sha256` is the SHA-256 of the exact emitted PNG bytes. There is **no pending key, no second object state, and no finalization copy**. The `view/{view_id}/` segment is removed from the export key grammar, and the "pending-compile-id" object state is removed with it.

The key is content-addressed, so writing the same derivative twice is the same write. Namespace, community, and case remain in the prefix, so the bucket policy and the demo reset manifest keep the scoping they already had.

### 2. The DynamoDB compile transaction is the sole authorization commit point

Before that transaction commits, an export object may exist and it **confers no authority**:

- no `ShareableCaseView` references it;
- no current-view pointer names a view that references it;
- nothing external can reach it, because reachability runs through a view's opaque `export_handle_id` and an authorized shareable-zone adapter, never through a key a caller can guess.

After the transaction commits, the immutable view and its `ShareableEvidenceRef` make an object that was already durable part of committed safe state.

If the transaction fails or is never attempted, the object is an unreferenced orphan. It is harmless by the paragraph above, no current view may ever reference it, and the export bucket's existing lifecycle backstop eventually removes it. This is the entire orphan story; there is no reconciliation job and no compensating delete on the compile path.

### 3. An unknown S3 PUT outcome is resolved by reading the key, never by writing a second one

```text
HEAD the exact content-addressed key
  present, and derivative hash, byte length, and frozen metadata all agree -> the write is present
  absent                                                                   -> repeat the identical PUT
  present and inconsistent                                                 -> IntegrityError, quoting nothing
```

A second key is never created because a PUT outcome was unknown. Content addressing is what makes the retry safe: the repeated PUT has the same key and the same bytes, so it is the same write rather than a second one.

### 4. The V1 image sanitizer profile, frozen

The decoder and encoder is **Pillow, pinned at exactly `12.3.0`**. That release publishes CPython 3.12 wheels for `win_amd64` and `manylinux_2_28_x86_64`, which covers local development and the Lambda Python 3.12 runtime, so no source build enters the supply chain. Implementation pins this exact version in `pyproject.toml` and `uv.lock`; changing it is an ADR.

| Property | Frozen V1 value |
|---|---|
| Accepted source media types | `image/jpeg`, `image/png`, and nothing else |
| Maximum source bytes | `10_000_000` bytes exactly |
| Frames | exactly one; animated or multi-frame input is rejected |
| Decoded pixel cap | `16_000_000` pixels |
| Maximum input dimension | `8192` on either axis |
| Truncated or malformed input | rejected; Pillow truncated-image loading stays disabled |
| Decompression bomb | fail closed; Pillow's bomb warning and error are both handled as rejection |
| Orientation | EXIF orientation applied deterministically **before** metadata is discarded |
| Alpha | composited deterministically onto opaque white |
| Working and output mode | `RGB` |
| Resize | aspect ratio preserved; longest output edge at most `2048` |
| Target format | PNG |
| PNG writer | `optimize=False`, `compress_level=9` |
| Carried through from the source | nothing: no EXIF, no ICC, no XMP, no comment or text chunks, no source metadata of any kind |
| `derivative_sha256` | SHA-256 over the exact emitted PNG bytes |

The two size caps bind independently and neither implies the other: `8192 x 8192` is `67_108_864` pixels, well past the pixel cap, and a `16_000_000`-pixel image can sit inside the dimension cap. Both are checked.

Orientation is applied before metadata is dropped because the two operations do not commute. Dropping EXIF first would silently lose the rotation the photograph was taken with, and the derivative would be a correctly sanitized picture of the wrong thing.

**The determinism claim is exact and bounded.** For the same source bytes and the same sanitizer settings, output is byte-identical **under the pinned CHORUS runtime and dependency set**. Bit identity across arbitrary future Pillow or zlib versions is *not* claimed, because PNG filter selection and deflate output are properties of the encoder build rather than of the format. Golden sanitizer and golden view-hash tests therefore pin the runtime and the fixture, and a dependency bump that moves a golden hash is a reviewed change rather than a surprise.

No OCR. No general document conversion. No visual model. Text documents and emails remain non-exportable in V1.

### 5. The fixed review is immutable curated demo-fixture metadata

In policy/v1 the safe-evidence review is a curated record carried in the elevator fixture manifest beside the evidence entry it describes. It is **not** an LLM judgement, not visual inference, not a moderation API, not a runtime service, and not a resident mandate. No other producer of a review exists.

The record's frozen fields are:

| Field | Meaning |
|---|---|
| `no_face` | boolean; no identifiable person appears |
| `no_unit` | boolean; no apartment or unit label appears |
| `no_name` | boolean; no personal name appears |
| `no_health` | boolean; no health detail appears |
| `safe_caption` | the exported caption, 1–300 characters, no `@`, no `http(s)://`, no `s3://` |
| `reviewed_by` | fixture-curation provenance |
| `reviewed_at` | UTC instant of the curation decision |

The review is bound to the evidence entry's **existing exact `sha256`** of the source bytes, which the manifest already carries and the loader already verifies. `human_reviewed` is true only when all four hold:

1. the manifest's source `sha256` equals the SHA-256 of the loaded source bytes;
2. all four flags are true;
3. `safe_caption` is present and passes the frozen caption validation;
4. the review metadata is complete.

A missing, incomplete, or mismatched review fails closed. `reviewed_by` is curation provenance, not a `ContributorId`, and asserts no resident or management authority — in particular it is not a verification source and does not affect [ADR-015](ADR-015-evidence-status-and-verification.md)'s empty allowed set.

### 6. One immutable compiler audit projection, in the Audit table

```text
PK  NS#{namespace}#CASE#{case_id}
SK  COMPILE#{compile_id}
```

Schema `compiler-audit-projection/v1`, immutable and create-only. One row per compile, written by the compile transaction on **both** `ALLOW` and `DENY`.

The Audit table is the only table it can live in. The frozen trust matrix in [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) grants the compiler `Audit: W`, `Share: W(view prefixes only)`, and `Core: W(fence only)`, so Core is unavailable; and the zone-separation contract forbids private *identifiers* in externally bound Shareable items, which this row is full of. Private identifiers in the Audit table are already contemplated — [08-api-design.md](../architecture/08-api-design.md) shows a compiler audit projection carrying a fact ID — and `tests/unit/persistence/test_zone_separation.py` checks Audit items for private *text* only.

The row carries identifiers, codes, versions, and digests. It carries **no** raw private text, report summary, fact value, evidence bytes, prompt, or completion.

`AuditEvent` is unchanged and stays the small append-only decision event; it references this row through its existing entity-reference fields. `AuditDetails` is **not** widened to duplicate the projection. There is no per-fact `AuditEvent`.

## Alternatives considered

- **Keep the `view_id` export key and add a post-commit finalization copy.** Rejected: it creates a failure mode in which a committed current view references an object that does not exist, and it needs a repair path, a new failure-matrix row, and a reconciliation story — all to preserve a key segment that identifies nothing the reference does not already identify.
- **Keep a `pending/` prefix and promote on commit.** Rejected for the same reason in a different shape. A promotion is a copy, a copy can fail, and the object's safety was never a property of its prefix: an unreferenced object is unreachable because nothing references it, not because of what it is called.
- **Delete orphan objects on transaction failure.** Rejected: a compensating delete is itself a write that can fail, so it converts a harmless orphan into a second ambiguous outcome, and the lifecycle rule already handles the case at no cost.
- **Create a second key when a PUT outcome is unknown.** Rejected outright: it manufactures divergent copies of one derivative, which is exactly what content addressing exists to prevent.
- **Leave the image library unnamed and let implementation choose.** Rejected: the golden-hash gate in [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) is a claim about a specific encoder's bytes, and R16 is a claim about a specific parser's attack surface. Neither can be reviewed against a library nobody has named.
- **Claim byte identity across Pillow versions.** Rejected as untrue. Naming the bound is worth more than an unqualified claim a future upgrade would quietly falsify.
- **Model the review as a stored human decision with its own write path.** Rejected for V1: it invents an authority mechanism, an actor, and a mutation surface for a corpus of exactly one photograph, and [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) already accepts fixed fixtures as the V1 answer to arbitrary uploads.
- **Reuse `ContributorId` for `reviewed_by`.** Rejected: it would write into private storage the falsehood that a resident authorized an export review, which is the same category of error [ADR-015](ADR-015-evidence-status-and-verification.md) refused when it declined to model management as a contributor.
- **One `AuditEvent` per included and excluded fact.** Rejected on an arithmetic bound rather than on taste: a hundred facts is reachable at the frozen per-case limits, and a hundred audit participants plus the compile's eight fixed ones exceeds DynamoDB's hundred-operation transaction limit.
- **Widen `AuditDetails` to carry per-fact scope and reason codes.** Rejected: it turns a closed bounded shape into a general bag, and it would duplicate the projection so the two could disagree.
- **Put the projection in the Core table beside the case.** Rejected: the compiler's only Core write is the send fence, and granting it a second one to store an audit artifact would trade the boundary for a filing convenience.

## Why chosen

Each decision removes a state rather than adding one. Content addressing removes the pending object, the finalization copy, and two failure modes; one commit point removes the question of which store is authoritative; a pinned profile removes the difference between two honest implementations of "deterministic"; fixture metadata removes a review authority nobody needs for one photograph; and a single projection row removes both the per-fact event explosion and the temptation to widen a closed audit shape.

Nothing here reopens a Phase-5 semantic. `EvidenceStatus`, the empty verified-source set, case versus fact corroboration, the ADR-017 closure service, and the 22-gate compiler are all untouched.

## Consequences

- The export key grammar in [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) loses its `view/{view_id}/` segment and gains `{derivative_sha256}`; the "pending" object state is deleted from § Transaction boundaries.
- Two failure-matrix rows in [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) are rewritten and one is added, for the unknown PUT outcome.
- `pyproject.toml` and `uv.lock` gain `pillow==12.3.0` **when implementation starts**, not before.
- The sanitizer is infrastructure and Pillow is an infrastructure dependency, so it may not import `chorus.privacy`: the object-store and sanitizer ports return plain domain values, and the application layer assembles `SafeEvidenceCandidate`. No import-linter exception is added.
- `ShareableEvidenceRef.media_type` becomes the constant `image/png`, because the reference describes the derivative and every accepted image is re-encoded. `chorus.privacy.compiler` currently passes the *source* item's media type into `build_safe_evidence_ref`; that is a defect, it is corrected in Phase 6, and no golden hash is written against the source MIME.
- The elevator manifest's evidence entries gain the seven review fields. The fixture reader consumes every key and fails closed on a leftover, so **the manifest edit and the loader change must land in the same commit**, at implementation start.
- The compile transaction gains the projection as a fixed participant, on both `ALLOW` and `DENY`.
- A codec test asserts that the largest legal `compiler-audit-projection/v1` row — a hundred facts and twenty evidence entries at the frozen maxima — stays safely inside DynamoDB's 400 KiB item limit.
- The frozen elevator photo fixture is a 1×1-pixel JPEG carrying only a JFIF marker, so it cannot demonstrate EXIF or GPS stripping. The metadata-removal tests construct their adversarial inputs in-test rather than relying on that fixture; no committed fixture gains real location data.

## Revisit condition

Revisit the content-addressed key only if a derivative ever needs to differ between two views of the same case from the same source bytes — which cannot happen while the sanitizer is deterministic and its settings are frozen, because identical inputs produce an identical hash and therefore an identical key. If a future transformation becomes view-dependent, the key gains the discriminator that actually makes it differ, recorded in a superseding ADR; it never regains a mutable pending state.

Revisit the Pillow pin on a security advisory affecting the accepted decoders, or on a determinism change that moves a golden hash. Either is a reviewed version change with re-cut goldens, never a floating range.

Revisit the fixture review only when V1's fixed-fixture assumption is itself revisited. A real review authority is a new actor, a new write path, and a new mandate question, and it requires its own ADR stating who may mark evidence safe and what that permission does not extend to.
