# ADR-021: Everything a model writes is cited, and the exact grammar that grounds it

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [03-agent-architecture.md](../architecture/03-agent-architecture.md) § Action Coordinator Agent; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Core enums, § ActionClaim, § ActionProposal; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Deterministic proposal validation, § Deterministic rendering; [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) § Named security tests; ADR-007 § Consequences; [ADR-015](ADR-015-evidence-status-and-verification.md) § 7's forward reference to the Action validator

## Context

The Phase-7 pre-implementation review found four things in the frozen Action contract that were named and never defined. Each is a place where an implementer would have had to invent a rule that decides whether an unsupported factual assertion reaches an external recipient.

### "Factual premise" had no test

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) check 6 requires that "every request/caveat factual premise has citations; caveats with no premise may have zero citations", and [03-agent-architecture.md](../architecture/03-agent-architecture.md) restates it: "`request.requested_action` is normative preference, not a factual claim; if it includes a factual premise it must cite IDs."

Nothing defines the test. The boundary case is obvious and undecidable as written:

```text
"Please repair the elevator."                              -> normative
"Because the elevator failed three times, repair it."      -> contains a premise
```

Distinguishing them is clause-level natural-language analysis. The only honest implementations are a parser nobody has specified or a model, and a model validating a model is the arrangement [01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md) exists to prevent.

### "Lexically supported" had no algorithm

Check 10 requires that "numbers, dates, quoted strings, and proper-name candidates in factual text must be lexically supported by at least one cited fact's `safe_text`". A repository-wide search returns that sentence, "deterministic lexical/semantic guards" in [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md), and ADR-007's warning that conservative checks may over-reject.

Undefined: tokenization; what counts as a number; whether `four` and `4` are the same claim; whether `14 January` and `2030-01-14` are; quote delimiters; the proper-name detector; case folding; which normalization form the comparison runs in; whether identifiers count as support.

[12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) already requires a golden test over the validator's behaviour, which is the same problem [ADR-018](ADR-018-safe-evidence-and-compile-commit.md) found with a golden hash over an unnamed image encoder: a golden is a statement about a specific algorithm, and there was no algorithm to be specific about.

### A caveat could not carry its own proof

`ActionProposalDraft` declares `caveats: tuple[{text, export_fact_ids[]}, ...]`. `chorus.domain.entities.ActionProposal` stores `caveats: tuple[str, ...]` — bare strings — and the codec round-trips them as bare strings. So the validated caveat-to-fact binding was discarded at persistence: the immutable artifact a human approves would not contain the proof the validator relied on, Phase-8 revalidation could not re-check it, and the renderer's References block could not cite it.

### The Phase-5 obligation had no enforcing check

[ADR-015](ADR-015-evidence-status-and-verification.md) § 7 places the LOW-contradiction caveat obligation squarely on this phase:

> The caveat obligation is discharged by the Action phase, not by Phase 5: contradicted facts carry `evidence_status=CONTRADICTED` into `ShareableFact`, and the Action proposal validator is where caveating them becomes mandatory.

The twelve checks in [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) contain no contradiction check at all, and nothing defines the obligation's scope — every contradicted fact in the view, or only the ones the proposal relies on.

## Decision

### 1. Every model-authored substantive field is citation-bound. There is no factual-premise classifier.

**The distinction is removed rather than implemented.**

| Field | Citations |
|---|---|
| `claims[].export_fact_ids` | `1..10`, unique, sorted |
| `request.request_fact_ids` | `1..10`, unique, sorted — **never zero in V1** |
| `caveats[].export_fact_ids` | `1..10`, unique, sorted — **never zero in V1** |

Every citation must name an `export_fact_id` present in the exact bound view. An empty citation set on any of the three is a whole-proposal rejection.

So `"Please repair the elevator"` remains a normative request — it asserts nothing — and it still cites the externally safe facts that justify asking. That is not a category error. A request without a reason is a request the recipient cannot evaluate, and the citations are what make the rendered `References` block complete for a request as well as for a claim.

This deletes the need for a premise classifier entirely. There is no sentence CHORUS must parse, because there is no field in which an uncited sentence can be persisted.

**Language permitting zero citations on a request or a caveat is removed from [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) check 6 and from [03-agent-architecture.md](../architecture/03-agent-architecture.md).**

### 2. `ActionCaveat` becomes structured

```text
ActionCaveat
  caveat_id: UUID              # model-local within one proposal, exactly as claim_id is
  text: str[1..500]
  export_fact_ids: sorted tuple[UUID, 1..10], unique
  caveat_hash: Sha256Digest
```

`ActionProposal.caveats` becomes `tuple[ActionCaveat, ...]`, `0..8`, with unique `caveat_id` values. `caveat_hash` is computed the way `claim_hash` is, through the existing canonical authority, and `proposal_hash` covers the whole structure.

`caveat_id` is UUID-shaped and model-local, which is the same deliberate exception `claim_id` already is: it names nothing outside its own proposal, survives no lookup, and grants nothing. The `reject_identifier_shaped` guard that applies to Monitor `client_ref` values does **not** apply to either.

`action-proposal/v1` becomes `action-proposal/v2`. No proposal rows exist anywhere, so this is a code change with re-cut fixtures.

### 3. The LOW-contradiction caveat obligation, made executable

The Action Agent still receives only the `ShareableCaseView`. **It never reads `InvestigationAssessment`**, and contradiction *materiality* is deliberately absent from the view — Phase 5 remains the sole authority that `MEDIUM` and `HIGH` contradictions block `READY_FOR_ACTION`, so any contradiction reaching a current view is `LOW` by construction, and Phase 7 does not re-derive that judgement.

```text
relied_fact_ids := union(claim.export_fact_ids for every claim)
                 ∪ request.request_fact_ids

for each f in relied_fact_ids:
    if view.fact(f).evidence_status == CONTRADICTED:
        require some caveat c with f ∈ c.export_fact_ids
```

A missing required caveat is a **whole-proposal rejection**, code `CONTRADICTED_FACT_NOT_CAVEATED`.

Two scope decisions, both deliberate:

- **Relied-upon, not present-in-view.** A contradicted fact the proposal actually asserts or leans on must be caveated. A contradicted fact that merely sits in the view, which the proposal never mentions, does not force the message to raise a doubt about a claim it declines to make. Requiring the second would make every proposal introduce material it had chosen to omit.
- **Caveat citations do not recurse.** A caveat citing a contradicted fact does not itself create a further obligation. Otherwise the only fixed point would be an infinite regress or an arbitrary depth limit.

`ShareableFact.evidence_status` is sufficient for this because `chorus.privacy.transformations` makes `CONTRADICTED` sticky through every aggregation: a transformed or aggregated fact whose inputs include a contradicted one is itself `CONTRADICTED`. Gate 19 already admits evidence status as minimum-necessary output, so the signal is guaranteed to travel.

### 4. Comparison normalization

Normalization exists **only for comparison**. It never rewrites persisted model text, and a proposal that passes is stored exactly as the model wrote it.

```text
normalize(s):
    1. s = NFC(s)
    2. s = casefold(s)
    3. s = NFC(s)                        # casefold can denormalize; re-fix it
    4. s = collapse every run of Unicode whitespace to one ASCII space
    5. s = strip leading and trailing spaces
```

Every support comparison in §6 runs between two `normalize()` outputs. Nothing else is normalized: no punctuation stripping, no hyphen folding, no diacritic removal, no stemming, no plural folding. Those are all forms of guessing that two different strings mean the same thing, which is the thing this ADR refuses to do.

### 5. Structural rejection, before any support checking

#### Scope: the four model-authored textual fields, and nothing else

Structural rejection runs on the NFC text of exactly four strings — `subject`, every `claims[].text`, `requested_action`, and every `caveats[].text` — **before** normalization and before grounding. Any match rejects the whole proposal.

**It does not run on the typed structural fields of the contract.** `case_id`, `view_id`, `view_hash`, `authorization_version`, `claim_id`, `caveat_id`, `export_fact_ids`, and `request_fact_ids` are typed contract data, not prose. They are validated by the ordinary schema, ownership, and current-view checks in [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) — that every cited ID exists in the exact bound view, that no foreign ID appears, that claim and caveat IDs are unique — and never by the prose scanner. Reading "identifiers are rejected outright" as a rule about the whole payload would make the contract reject its own required identifiers.

| Class | Rule |
|---|---|
| Encoding | any lone surrogate, or any byte sequence that is not strict UTF-8 |
| Control characters | any `U+0000`–`U+001F`, `U+007F`–`U+009F`. This subsumes CR/LF header injection. |
| Bidi controls | `U+061C`, `U+200E`, `U+200F`, `U+202A`–`U+202E`, `U+2066`–`U+2069` |
| Invisible formatting | `U+00AD`, `U+200B`, `U+200C`, `U+200D`, `U+2060`, `U+FEFF` |
| Markup | any `<` or `>`; any HTML entity `&[#0-9A-Za-z]{1,10};` |
| Markdown link/image | `![` or `](` |
| URL | `(?i)\b[a-z][a-z0-9+.\-]*://`, or `(?i)\bwww\.` |
| `mailto:` | `(?i)\bmailto:` — a **separate** rule, because a `mailto:` URI has no `//` and the URL pattern above therefore never matches one |
| Email address | `(?i)[^\s@]+@[^\s@]+\.[a-z]{2,}` |
| Telephone | `\+?[0-9][0-9 \-().]{5,}[0-9]`, subject to the ISO-date precedence below |
| Apartment or unit | `(?i)\b(?:apt|apartment|unit|suite|ste|flat)\s*#?\s*[0-9]{1,5}[a-z]?\b`, or `#\s*[0-9]{1,5}[a-z]?\b` |
| Identifier shapes | any UUID, any `sha256:[0-9a-f]{64}`, **appearing in one of the four textual fields** |
| Quotation | any `"`, `` ` ``, `“`, `”`, `„`, `‘`, `«`, `»`; and any `'` or `’` **not** between two word characters |
| Sensitive terms | any match of the existing `chorus.privacy.compiler._UNSAFE_VALUE` pattern — private sentinels, `s3://`, relationship and health vocabulary, and the contact and unit patterns it already carries |

#### The telephone rule and the ISO-date rule, in precedence order

The frozen telephone candidate matches `2030-01-14`, and § 6 makes `YYYY-MM-DD` the *only* date a model may write. Because structural rejection runs before grounding, an unordered reading of the two rules would reject every supported date. The order is therefore normative.

**Step A — identify valid ISO-date spans first.** The syntactic candidate is

```text
(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])
```

and every candidate is then validated as a real Gregorian calendar date by the runtime's standard deterministic ISO parser — the behaviour of `datetime.date.fromisoformat` on that exact substring. A candidate that does not parse is not an ISO-date span.

```text
2030-01-14   valid          2030-02-29   invalid (1900-rule non-leap year)
2032-02-29   valid          2030-13-01   invalid (month 13)
```

**Step B — run the telephone candidate.** For each telephone match:

- **if and only if the entire matched span is exactly one already-validated ISO-date span**, it is not a telephone number. It is left to the ordinary date-grounding rule of § 6;
- **otherwise** it is rejected as `PHONE_PATTERN`.

**There is no substring exemption.** A telephone match that merely *contains* a valid ISO date, or overlaps one, is still a telephone number.

```text
2030-01-14                not a phone; proceeds to the § 6 date support check
call 2030-01-14           same
555-123-4567              phone -> reject
+1 (555) 123-4567         phone -> reject
2030-99-99                not a valid date; phone candidate -> reject
12345678                  phone candidate -> reject
```

**The exemption is from `PHONE_PATTERN` only; it authorizes nothing.** A date that survives step B must still be matched exactly by an `ISO_DATE` token in a cited `ShareableFact.safe_text` under § 6, and no date is ever reformatted to make a match.

Three of these deserve their reason recorded.

**Quotation is banned rather than grounded.** Deciding whether a quoted string is faithfully drawn from a cited fact is entailment over a span, and the alternative — exact substring equality against `safe_text` — would silently teach the model that quoting safe text verbatim is the way to pass, which is a re-identification vector gate 18 already refuses. A word-internal apostrophe stays legal so `elevator's` works.

**`mailto:` is its own rule and not a case of the URL rule.** `https://example.com` has an authority component and matches `[a-z][a-z0-9+.\-]*://`; `mailto:user@example.com` has none and matches nothing in that pattern. The frozen validator contract has always required `mailto:` rejection, and folding it into "a URL" would have quietly dropped it. `MAILTO:` in any casing is the same rule. The email-address pattern remains a second, independent defence against the address itself.

**The sensitive-term rule is absolute, and the frozen "not present in a safe fact" exception is vacuous.** Compiler gate 21 runs the same `_UNSAFE_VALUE` scanner over the constructed view and denies the whole compile on a match, so no current view can contain text matching it. The exception could only ever have been satisfied by a view that cannot exist. Phase 7 therefore rejects absolutely and reuses the compiler's pattern rather than writing a second one — the reuse [ADR-018](ADR-018-safe-evidence-and-compile-commit.md) already licensed for the recursive safe-field scanner.

### 6. Risk tokens and exact support

Risk tokens are extracted from normalized text by **one ordered alternation**, scanned left to right with non-overlapping matches, so an earlier alternative consumes its span and a later one cannot re-match inside it. Order is normative:

```text
1. ISO_DATE          [0-9]{4}-[0-9]{2}-[0-9]{2}
2. CLOCK_TIME        [0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?
3. ORDINAL_NUMERIC   [0-9]+(?:st|nd|rd|th)
4. NUMERIC           [0-9]+(?:[.,][0-9]+)*%?
5. NUMBER_WORD       one member of the closed word list below
```

`ISO_DATE` precedes `NUMERIC` so that `2030`, `01`, and `14` inside `2030-01-14` are not extracted as three separate numbers.

**Support is match-to-match equality, never substring containment.** A risk token `t` extracted from a model field is supported when some cited fact's normalized `safe_text` yields, under the same alternation, a token exactly equal to `t`.

Substring containment is refused because it accepts in the dangerous direction: `4` is a substring of `24`, and `2030-01-14` contains `01`. Match equality means a supported number is a number the safe fact actually stated.

**No cross-form conversion, in either direction.** These are all unsupported unless the exact form appears:

```text
"four"  is not supported by "4"          "4"    is not supported by "four"
"04"    is not supported by "4"          "4.0"  is not supported by "4"
"14 January 2030" is rejected outright (see below)
```

**`NUMBER_WORD` is a closed list**, and it exists to close an evasion rather than to add semantics: without it a model could write "the elevator failed four times" and pass a check that only looks at digits.

```text
zero one two three four five six seven eight nine ten eleven twelve
thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty
thirty forty fifty sixty seventy eighty ninety hundred thousand
first second third fourth fifth sixth seventh eighth ninth tenth
eleventh twelfth thirteenth fourteenth fifteenth sixteenth
seventeenth eighteenth nineteenth twentieth
```

A `NUMBER_WORD` is supported only by the identical word in a cited `safe_text`. `second` is on the list in its ordinal sense and is treated as a risk token in every sense; a model wanting the time unit should not be writing one.

**Dates have exactly one permitted external form.** `YYYY-MM-DD` is the only date a model may write, and it must be supported. Every other date construct is **rejected outright**, not grounded:

```text
(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+[0-9]{1,2}\b
(?i)\b[0-9]{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b
\b[0-9]{1,4}/[0-9]{1,2}/[0-9]{1,4}\b
(?i)\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b
(?i)\b(?:yesterday|today|tomorrow|last\s+(?:week|month)|this\s+(?:week|month))\b
```

Rejecting rather than grounding is what removes format-equivalence reasoning. It is also consistent with what the view actually contains: `chorus.privacy.transformations` renders incident dates through `date.isoformat()`, and a range as `"{first} to {last}"`, so ISO is the only date form a safe fact ever carries. Relative expressions are rejected because their referent is the reading time, which is not a fact the view can support.

### 7. Proper-name candidates

The detector runs on the **NFC text before casefolding**, because capitalization is the signal.

```text
CAPITALIZED_WORD := \p{Lu}[\p{L}\p{M}'’\-]{1,}
NAME_RUN         := a maximal sequence of one or more CAPITALIZED_WORDs
                    separated by single spaces
```

`\p{Lu}`, `\p{L}`, and `\p{M}` are **Unicode general-category notation defining the grammar**, not a mandate to add a regex engine that understands that syntax. Implementation may realize exactly this grammar with `unicodedata.category` or any already-approved equivalent, provided tests prove the frozen category semantics — an uppercase-letter start, and letters, combining marks, apostrophes, and hyphens thereafter. **No new dependency is introduced by this ADR**, and adding one would be a separate reviewed change.

A `NAME_RUN` is a **candidate** unless it is a single word in sentence-initial position, where sentence-initial means the first word of the field or the first word after `.`, `!`, or `?` followed by whitespace.

This is deliberately a positional rule and not a stop-word list. A stop-word list is the arbitrary lexical heuristic [ADR-012](ADR-012-candidate-grouping-invariant.md) refused, and it would need maintaining in every language the demo might ever show. Position is decidable from the string.

```text
"The elevator failed."          -> "The" is a single sentence-initial word; not a candidate
"Bob Smith should be called."   -> "Bob Smith" is a two-word run; candidate
"Please contact Bob Smith."     -> non-initial; candidate
"…in Building B."               -> "Building B"; candidate
"…on Monday."                   -> rejected earlier as a weekday, never reaching this stage
```

A candidate is supported when its normalized form occurs as a **normalized substring** of at least one of:

1. a cited `ShareableFact.safe_text` for that field;
2. `view.destination.display_label`;
3. `view.community_public_label`;
4. the closed reviewed template-copy allowlist, which in V1 is exactly `{"Ambient CHORUS", "CHORUS"}`.

**Substring is correct here and match-equality was correct for numbers, and the asymmetry is the point.** A numeric substring match accepts a *different quantity* (`4` from `24`), which is a false factual assertion. A name substring match can only accept a fragment of a name the view already publishes, which asserts nothing new.

Sources 2 and 3 are named explicitly because the destination label and the community label are the two names a proposal legitimately needs and neither is a fact with an `export_fact_id`.

**Identifiers never support anything.** A UUID, a `sha256:` digest, a `view_id`, an `export_fact_id`, a `routing_token`, or a `case_id` is neither a supportable token nor a source of support: a proposal cannot become grounded by naming an identifier, and an identifier appearing in the model's prose is already rejected by § 5.

That rejection is about **prose**, and the distinction matters enough to restate it here. `case_id`, `view_id`, `view_hash`, `claim_id`, `caveat_id`, `export_fact_ids`, and `request_fact_ids` are required typed contract fields; they are validated for view and case membership by the ordinary checks and are never fed to the prose scanner. What is forbidden is copying one of them into `subject`, a claim, the request, or a caveat — where it would travel outward as text, ground nothing, and expose an internal handle in an external message. The renderer emits reference markers and short export-fact IDs as its own template copy; that is renderer output, not model text, and it is not subject to this rule either.

### 8. Subject grounding

`subject` has no citation field of its own, so its support context is frozen as the union of:

- every `claims[].export_fact_ids`;
- `request.request_fact_ids`;
- `view.destination.display_label` and `view.community_public_label`;
- the template-copy allowlist.

Every §5 structural rule and every §6 and §7 support rule applies to the subject unchanged.

`subject` is **1–120 Unicode characters**. `chorus.domain.entities.ActionProposal` currently enforces 1–200, which contradicts both [03-agent-architecture.md](../architecture/03-agent-architecture.md) and [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md); the entity is corrected to 120.

### 9. Duplicate detection

Two claims duplicate when their normalized texts are equal. Two caveats duplicate when their normalized texts are equal. Either is a whole-proposal rejection.

Equal citation sets across two claims with different normalized text are **legal**: two distinct statements may rest on the same fact.

### 10. `requested_deadline`

It is a normative preference and carries no lexical obligation — it is a typed UTC instant, not prose. It must be timezone-aware UTC and strictly after `view.generated_at`. The renderer prints it at day precision, matching the day precision the compiler's own date transformations use. The human preview is the check on whether the date asked for is reasonable.

### 11. `tone` becomes a closed enum

`ActionTone` with exactly `NEUTRAL`, `COLLABORATIVE`, `FIRM`, promoted from the free `str` that `chorus.domain.entities.ActionProposal` carries today. The free string has already drifted: a Phase-1 hash test constructs `tone="PROFESSIONAL"`, a value outside the frozen set that nothing refused.

The renderer consumes `tone` only to select frozen template copy. It never reaches the model's own text and grants nothing.

### 12. False positives

**A false positive rejects the proposal and requires a re-proposal. It is never bypassed, never overridden, and never adjudicated by a second model.** ADR-007 already accepted this cost, and [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md)'s support-precision target of 1.00 is met by rejecting, not by loosening.

The rules above will reject some good copy — a legitimate mid-sentence capitalized common noun, a spelled-out number the safe fact wrote in digits, a date the model chose to render in prose. That is the accepted direction. R21 already records over-rejection as an accepted trade-off, and the prompt tells the model exactly which forms pass, so the ordinary path does not depend on luck.

**No embeddings. No semantic similarity. No entailment engine. No second LLM validator.** The validator is pure functions over strings and the bound view.

### 13. Worked examples

The view for these examples contains one cited fact with
`safe_text = "The elevator was out of service on 2030-01-14, 2030-01-15 and 2030-01-19; 4 residents reported an impact."`,
`display_label = "Property Management"`, and `community_public_label = "Maple Court"`.

| Model text | Verdict | Reason |
|---|---|---|
| `The elevator was out of service on 2030-01-14.` | accept | `2030-01-14` matches a cited `ISO_DATE` |
| `4 residents reported an impact.` | accept | `4` matches a cited `NUMERIC` |
| `Four residents reported an impact.` | reject | `four` is a `NUMBER_WORD`; the cited fact says `4` and no conversion exists |
| `The elevator failed on 14 January.` | reject | prose date form, rejected outright |
| `24 residents reported an impact.` | reject | `24` is not a cited token; substring containment is not support |
| `Residents of Maple Court are affected.` | accept | `Maple Court` is the community public label |
| `Please write to Property Management.` | accept | `Property Management` is the destination display label |
| `Bob Smith reported the outage.` | reject | unsupported name candidate |
| `The elevator's door jammed.` | accept | word-internal apostrophe is legal; no risk token |
| `A resident said "it stopped again".` | reject | quotation |
| `See https://example.com/report` | reject | URL |
| `Write to mailto:pm@example.com` | reject | `MAILTO_PATTERN`, and the email pattern independently |
| `Call 555-123-4567 to confirm.` | reject | `PHONE_PATTERN`; the span is not a valid ISO date |
| `Please confirm by 2030-01-19.` | accept | telephone candidate, but the whole span is a validated ISO date and it is cited |
| `The outage began 2030-02-29.` | reject | not a real calendar date, so the phone exemption does not apply; `PHONE_PATTERN` |
| `Reference 3f2a9c11-0b7e-4d18-9a52-1c7f0e6b84d3.` | reject | identifier shape in prose |
| A proposal whose `view_id` is a UUID | accept | typed contract field, validated by view membership, never by the prose scanner |
| `Contact unit 4B.` | reject | unit pattern |
| `Please repair the elevator.` (cites the fact) | accept | normative, cited, no risk token |
| `Please repair the elevator.` (cites nothing) | reject | `request_fact_ids` may not be empty |
| `The elevator was out of service three times.` | reject | `three` unsupported; the fact lists dates, not a count word |
| A claim citing a `CONTRADICTED` fact, with no caveat citing it | reject | `CONTRADICTED_FACT_NOT_CAVEATED` |

## Alternatives considered

- **Implement a factual-premise classifier.** Rejected: the only deterministic version is a clause parser nobody has specified, and the only accurate version is a model. Requiring citations everywhere makes the question disappear, which is cheaper than answering it.
- **Keep zero-citation caveats for "non-factual" caveats.** Rejected for the same reason: it reintroduces the classifier through a side door, and a caveat with no citation is a sentence the recipient cannot trace, which is what structured claims exist to prevent.
- **Ground quoted strings by exact substring equality against `safe_text`.** Rejected: it would make verbatim quotation of safe text the reliable way to pass validation, and gate 18 already refuses direct quotes as a re-identification vector.
- **Allow `four` ↔ `4` and `14 January` ↔ `2030-01-14` equivalence tables.** Rejected: each table is a small semantic engine with locale, ordinal, and range edge cases, and every entry is an opportunity for two implementations to disagree. Rejecting the ambiguous form and telling the model which form to use costs one prompt sentence.
- **A stop-word list for proper-name detection.** Rejected: [ADR-012](ADR-012-candidate-grouping-invariant.md) refused exactly this heuristic, and a positional rule needs no vocabulary and no maintenance.
- **Substring containment for numeric support.** Rejected: it accepts `4` from `24`, which is a false quantity in an external message.
- **Match-equality for name support.** Rejected in the other direction: it would reject `Maple Court` against a label of `Maple Court Residents Association`, with no safety gain, because a fragment of a published name asserts nothing.
- **Ground `requested_deadline` against cited facts.** Rejected: it is the sender's request, not the case's evidence. A deadline supported by the past is not a coherent requirement.
- **Let a second model adjudicate a suspected false positive.** Rejected outright, and named here so it is refused once rather than proposed repeatedly. It would make the boundary probabilistic, which is the property the deterministic validator exists to remove.

## Why chosen

Each decision removes a question rather than answering it. Universal citation removes the premise classifier. One permitted date form removes format equivalence. Match equality removes substring false acceptance. Banning quotation removes span entailment. A positional name rule removes a word list. An absolute sensitive-term rule removes a conditional that could never have been satisfied. What remains is a closed set of regexes and two comparison rules, which two implementers can write the same way and a golden test can meaningfully pin.

It also keeps the Action Agent's authority exactly where [ADR-003](ADR-003-action-runtime-isolation.md) put it. The model contributes wording; every factual token in that wording must already exist in a compiled safe fact the model was given; and the one thing it cannot do is introduce a quantity, a date, or a name that nobody authorized.

## Consequences

- `chorus.contracts.action` declares `1..10` citations on claims, request, and caveats, with no zero-citation path.
- `ActionCaveat` is added to `chorus.domain.entities`; `ActionProposal.caveats` becomes structured; `chorus.privacy.canonical` gains `hash_action_caveat`; `action-proposal/v1` becomes `/v2`.
- `ActionTone` is added to `chorus.domain.entities`; the Phase-1 canonical hash test's `"PROFESSIONAL"` fixture is corrected.
- `ActionProposal.subject` is bounded 1–120.
- A new `chorus.application.services.action_grounding` module owns normalization, structural rejection, extraction, and support. It is pure over strings and the bound view and takes no repository. It applies to the four model-authored textual fields only; the typed contract identifiers are validated by the ordinary schema and view-membership checks.
- The telephone rule is implemented **after** ISO-date span validation, and its whole-span exemption is covered by tests over `2030-01-14`, `2030-02-29`, `2030-99-99`, `555-123-4567`, and `+1 (555) 123-4567`.
- The proper-name grammar is realized with `unicodedata.category` or an already-approved equivalent, with tests over the frozen Unicode categories. **No regex or Unicode dependency is added**; `pyproject.toml` and `uv.lock` are untouched by this ADR.
- `chorus.privacy.compiler._UNSAFE_VALUE` is exposed for reuse by the application layer, a permitted import direction; no second denylist is written and no import-linter exception is added.
- A new closed `ActionRejection` enum sits beside `AgentRejection` and `InvestigationRejection`, carrying at minimum `SCHEMA_INVALID`, `ENVELOPE_MISMATCH`, `PROMPT_VERSION_MISMATCH`, `VIEW_MISMATCH`, `STALE_VIEW`, `UNKNOWN_EXPORT_FACT_ID`, `FOREIGN_IDENTIFIER`, `EMPTY_CITATION_SET`, `DUPLICATE_CLAIM_ID`, `DUPLICATE_NORMALIZED_TEXT`, `UNSUPPORTED_TOKEN`, `REJECTED_CONSTRUCT`, `PHONE_PATTERN`, `MAILTO_PATTERN`, `CONTRADICTED_FACT_NOT_CAVEATED`, and `OUTPUT_EXCEEDS_BOUNDS`. Every member refuses the **whole** proposal; there is no per-claim salvage, for the reason `InvestigationRejection` already records.
- The `action/v1` prompt states the citation obligation, the permitted date form, the ban on quotation and on inventing quantities or names, and that view text is untrusted data. Because no Action runtime artifact has ever existed, `action/v1` names this artifact from the start; there is no version to bump.
- ADR-007's consequence "conservative lexical checks may reject good proposals" is now specific rather than anticipated; R21 and R22 in the risk register are updated to name the frozen algorithm.
- [ADR-015](ADR-015-evidence-status-and-verification.md) § 7's forward reference is discharged by § 3 above. `EvidenceStatus`, the empty verified-source set, contradiction materiality, and the readiness predicate are all untouched.

## Revisit condition

Revisit the token grammar only with measured evidence from the evaluation corpus that a specific rule over-rejects, and then by making that rule *more specific*, never by adding a similarity threshold. A change that makes more claims look supported is a change that makes weaker grounding look stronger, which is the failure this ADR exists to prevent.

Revisit the quotation ban only alongside a re-identification analysis of verbatim safe-text quotation, in a superseding ADR that states which spans may be quoted and where that permission stops.

Revisit universal citation only if a field is ever introduced that genuinely cannot be traced to a fact. The answer then is a new field with its own rule, not a nullable citation set on an existing one.
