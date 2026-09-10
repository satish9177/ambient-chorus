# ADR-030: The live SES receipt path — one S3 action, a pinned object, and the three fields the schema actually publishes

**Status:** Accepted
**Date:** 2026-09-08
**Accepted:** 2026-09-10 — Phase 11 Macro B, after independent review against ADR-025, ADR-026, the deployment contract, the current inbound attester, and installed `aws-cdk-lib` / `botocore` SES + AgentCore service models.
**Deciders:** Ambient CHORUS maintainers and product owner
**Supersedes:** [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) **only** in respect of the SES receipt transport shape and three decoded field choices — § 2 step 5 (which envelope fields are read), § 3 agreement 4 (which address the correspondent comparison uses), and § 3 agreement 5 (which address the recipient comparison uses). Everything else in ADR-026 is unchanged and remains in force: the four-stage boundary, the verdict gate, the attester/verifier split and its HMAC, the frozen refusal codes, the immutable outbound locator, the `SEND_UNKNOWN`-has-no-locator consequence, replay and idempotency behaviour, the raw-MIME-to-private-bucket rule, the attachment refusal, the size caps, the quoted-outbound-text removal, and the empty allowed-verification-source set. `Message-ID` correlation remains a live-canary question, not a redesign.
**Depends on:** [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md)

## Context

[ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) § 2 step 5 froze the decode step against
a described SES receipt envelope:

> Every field is read out of the SES receipt envelope and none is caller-supplied:
> `mail.commonHeaders.messageId`, `.inReplyTo`, `.references`, `.subject` …, `mail.source`
> (envelope MAIL FROM), `mail.destination` (envelope RCPT TO) …

`chorus.application.services.inbound_mail` implements exactly that. Phase 9 could not check it
against a live delivery, and Phase 11's job is to build the transport that produces one. Three of
those field choices do not survive contact with the real schema, and the transport shape the
deployment contract first proposed does not either.

**`inReplyTo` and `references` are not `commonHeaders` fields.** SES's `commonHeaders` object
carries a fixed set — `returnPath`, `from`, `date`, `to`, `cc`, `bcc`, `sender`, `replyTo`,
`messageId`, `subject`. The thread headers this entire correlation mechanism depends on are not
among them. `_message_ids(headers.get("inReplyTo"))` therefore reads `None` on every real
delivery, `thread_references` is empty, and every authenticated reply refuses with
`REPLY_UNCORRELATED`. The boundary fails closed, which is the right direction, but it fails closed
on *everything*.

**`mail.source` is the envelope MAIL FROM, and that is a return path, not a correspondent.**
Agreement 4 compares `normalize(mail.source)` against the destination's `address_digest`. For mail
composed in an ordinary client and relayed by an ordinary provider, MAIL FROM is routinely a
bounce address — SRS-rewritten, `bounces+…@`, or a subdomain the sender never types. It is also
the one field a relay is *expected* to rewrite.

**`mail.destination` is not the delivery authority.** It is the list of addresses derived from the
message's own destination headers, and the code additionally refuses any envelope carrying more
than one. A reply that CCs anybody — which a mail client does by default when replying to a
threaded message — refuses at `MALFORMED_ENVELOPE` before its verdicts are consulted. SES
separately publishes `receipt.recipients`: the addresses **matched by the receipt rule**, which is
what actually says "this arrived at an address we own".

**And a two-action transport hands the notification the wrong object.** A receipt rule with an S3
action followed by a separate SNS action produces a notification whose `receipt.action` describes
the *SNS* action; it does not carry the S3 action's `bucketName` and `objectKey`. The attester
would have nothing to fetch.

None of this weakens the trust argument. SPF, DKIM, and DMARC are still computed by SES over the
delivered message and still gate everything. What changes is the transport shape, which object the
attester reads, and which decoded field each identity comparison uses.

## Decision

### 1. One S3 action, carrying its own topic

The receipt rule has **exactly one action**, an `S3Action` configured with its own `TopicArn`:

```text
MX for {inbound-subdomain}
   -> SES inbound receiving (us-east-1)
   receipt rule set   chorus-demo-inbound
     receipt rule     chorus-demo-reply          (recipient: the one receiving address)
       S3Action(
         BucketName       = chorus-private-evidence-demo
         ObjectKeyPrefix  = ns/DEMO/inbound/
         TopicArn         = arn:aws:sns:us-east-1:{account}:chorus-demo-inbound-receipt
       )
   -> SNS topic  chorus-demo-inbound-receipt
   -> SNS subscription  ->  inbound Lambda entry point
```

**There is no separate SNS receipt action.** The notification SES publishes through the S3
action's own `TopicArn` describes *that* action, so `receipt.action.bucketName` and
`receipt.action.objectKey` name the object the action just wrote. That is the whole reason for
this shape.

The attester **requires `receipt.action.type == "S3"`** and refuses any other action type as
`MALFORMED_ENVELOPE`. A notification describing an action that did not write an object is a
notification with no object to bind to.

**Every policy and every configured comparison uses the full receipt-rule ARN**, not the rule-set
ARN:

```text
arn:aws:ses:us-east-1:{account}:receipt-rule-set/chorus-demo-inbound:receipt-rule/chorus-demo-reply
```

paired with `aws:SourceAccount` wherever the condition key is supported. A rule-set ARN authorizes
every rule in the set, including rules added later; the rule ARN authorizes the one rule this
deployment reasoned about. `CHORUS_INBOUND_SOURCE_ARN` holds the **rule** ARN.

### 2. The outer transport is authenticated by AWS, not by the body

**Provenance is established by AWS resource policies and trusted deployment configuration.**
The only permitted chain is the exact SES receipt rule → its S3Action with the configured
TopicArn → that SNS topic → the configured subscription and inbound Lambda. The SNS event's
TopicArn is a consistency check within this authorized chain, never independent proof of origin.

The locked chain, each link pinned by a resource policy:

| Link | Pinned by |
|---|---|
| SES → SNS | the topic policy admits `Principal: ses.amazonaws.com` only, conditioned on `aws:SourceAccount` = the account and `aws:SourceArn` = the full receipt-rule ARN |
| SNS → Lambda | the function's resource policy admits `Principal: sns.amazonaws.com` conditioned on `aws:SourceArn` = the exact topic ARN; no other principal may invoke this entry point |
| SES → S3 | the bucket write policy admits only the SES service, expected account, full receipt-rule ARN, and ingress prefix (§ 7) |
| Lambda → attester | the adapter validates the SNS event envelope against the configured topic and constructs the expected SES provenance from trusted configuration, under the exclusive delivery chain above |

These restrictions apply to **effective permissions**, not only one Allow statement: the SNS
topic policy explicitly denies other publishers and wrong SES account/rule sources, and no
same-account identity policy may authorize direct invocation of this inbound function or bypass
the publish restriction. Deployment assertions and negative invocation/publish canaries prove
that forged SNS-shaped input cannot enter through an alternate authorized caller.

Keep these three values separate:

| Value | Origin and use |
|---|---|
| `EXPECTED_RECEIPT_RULE_ARN` (`CHORUS_INBOUND_SOURCE_ARN`) | trusted deployment configuration; the full rule ARN enforced by SNS publish and S3 write policies and recorded as expected SES provenance |
| `Records[i].Sns.TopicArn` | SNS event-envelope value; must equal the separately configured expected ingress topic ARN |
| `receipt.action` | decoded SES notification data; must describe `S3` and carry the permitted bucket/key |

Python Lambda's `context` object supplies **no SNS TopicArn or source metadata**. The SES
notification supplies **no receipt-rule ARN field**. Neither value is invented or read from a
caller-supplied substitute. The trusted rule ARN is not compared with the SNS topic ARN.

At runtime, receive the normal SNS-trigger event, decode its outer envelope, require
`EventSource == "aws:sns"` and the exact configured `Records[i].Sns.TopicArn`, then decode
`Records[i].Sns.Message` as the SES receiving notification. Require the existing receipt-verdict
gates, `receipt.action.type == "S3"`, the configured bucket/prefix, and a matched
`receipt.recipients` address (using the configured digest comparison in § 6). Then continue
through the existing pinned-object, correlation, attestation, and admission stages. The detailed
attester order in § 4 is unchanged; decoding the outer transport envelope is the adapter's
consistency check, not admission or sender authentication.

The adapter constructs `InboundMailTransportContext` with the configured logical transport
`aws:ses-receipt` and expected full rule ARN only within this policy-constrained ingress path.
Copying these strings into JSON proves nothing. An arbitrary caller cannot acquire authority by
matching the topic, rule, bucket, recipient, or verdict strings.

AWS documents the [Lambda context fields](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html),
[SNS event envelope](https://docs.aws.amazon.com/lambda/latest/dg/with-sns.html), and
[SES receipt fields](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-notifications-contents.html).

### 3. The pinned object: one bounded read, one hash, one set of bytes

The receipt-derived `bucketName` and `objectKey` are the **only** object locator admitted. Nothing
else may name the bytes: not a caller, not a later event, not a second listing.

**Before any parsing or admission:**

1. **Validate the locator.** `bucketName` must equal the configured private evidence bucket and
   `objectKey` must lie under the configured `ns/DEMO/inbound/` prefix. Anything else is
   `MALFORMED_ENVELOPE`, refused before a byte is fetched.
2. **One bounded read.** A single `GetObject` on that exact bucket and key, reading at most
   `MAX_INBOUND_REPLY_BYTES + 1` bytes. More than the cap is `REPLY_TOO_LARGE`, refused whole, and
   the bytes are not retained. A missing object is `MALFORMED_ENVELOPE`.
3. **Pin the object identity.** The response's `VersionId` and `ETag` are captured from that same
   read. The receipt event does not carry a `VersionId`, so **the first trusted `GetObject` is
   what resolves and pins it**; there is no earlier moment at which it could have been known, and
   no later read is permitted to re-resolve it.
4. **Hash exactly the bytes that were read.** `sha256` is computed over the in-memory buffer from
   step 2 — not over a re-read, not over a `HeadObject` checksum, not over anything the service
   reported.
5. **Those same bytes are everything downstream.** MIME parsing, the `text/plain` extraction, the
   thread-header fallback of § 4, the persisted `EvidenceItem.sha256` and `byte_length`, the
   content-addressed evidence root, and the attestation all consume the one buffer. **There is
   never a second read of the object**, so there is no window in which the bytes that were hashed
   and the bytes that were parsed could differ.
6. **Reject any inconsistency** between the receipt metadata, the pinned object, and the parsed
   message — a `Message-ID` in the MIME that disagrees with `mail.commonHeaders.messageId`, a
   byte length that disagrees with what was read, or a hash that disagrees with an already-recorded
   root for the same message ID. Each fails closed and persists nothing in the case.

**The storage strategy that makes substitution resistible**, and it is a property of the bucket,
not a hope about timing:

- **Ingress bucket versioning is enabled** (it already is on the private evidence bucket), so an
  overwrite creates a new version rather than replacing bytes, and the pinned `VersionId` continues
  to name what was read.
- **SES writes under a service-generated object key** derived from the message, so ordinary
  delivery is create-only; a redelivery of the same message collapses through the existing
  content-addressed evidence root.
- **The bucket policy denies the application principals `s3:PutObject`, `s3:DeleteObject`, and
  `s3:DeleteObjectVersion` under `ns/DEMO/inbound/*`.** The inbound entry point holds `s3:GetObject`
  on that prefix and nothing else. Ingestion cannot replace the bytes it is reasoning about, and
  that is an IAM fact rather than a code convention.
- **Only the operational reset principal and the bucket lifecycle rule may remove ingress objects**
  ([the deployment contract](../plans/phase-11-deployment-contract.md) § 12).

"The bucket and prefix matched" is explicitly **not** sufficient and is not claimed to be.

### 4. Thread references, from the pinned bytes when the envelope lacks them

`In-Reply-To` and `References` are read, in order, from:

1. **`mail.headers`** — the full `[{name, value}]` collection SES publishes, matched
   case-insensitively on the RFC 5322 field names;
2. failing that, **the pinned bytes of § 3**, parsed with the standard library's header parser over
   the header block only and never the body.

**The fallback parses the same bounded, hash-pinned buffer that admission used.** It never issues a
second `GetObject`, and it never reads unpinned content to obtain a header — a header taken from
bytes nobody hashed is a header nobody can attest to.

`headersTruncated == true` remains `REPLY_HEADERS_TRUNCATED` and is checked **before** either
source is read: a truncated header set may have dropped the very `References` this depends on, and
reading a partial one is worse than refusing. If neither source yields a reference, the outcome is
the existing `REPLY_UNCORRELATED`.

`mail.commonHeaders.messageId` is unchanged — that field *is* published there.

**Ordering.** ADR-026 § 2's frozen order is preserved and made precise where it was silent:
authenticate → verdict gate → decode envelope → **pin and hash the object (§ 3)** → resolve thread
references → correlate → attest. The pinned read sits after decode because the locator comes from
the envelope, and before correlate because correlation consumes the references it may produce.
Nothing is decoded before the transport is authenticated, and nothing is admitted before it is
hashed.

### 5. Agreement 4 compares the parsed `From` mailbox — and what that does and does not prove

Agreement 4 becomes:

> `sha256("inbound-reply-party/v1" | namespace | normalize(from_mailbox))` equals the
> `address_digest` of the locator's `{destination_id, registry_version, routing_token}` triple,

where `from_mailbox` is the single RFC 5322 addr-spec in `mail.commonHeaders.from`. Absent, empty,
or more than one address is `MALFORMED_ENVELOPE` — a message claiming two authors is not a message
with an author.

**The authentication claim, stated accurately:**

- **SPF** authenticates the envelope MAIL FROM domain. **DKIM** authenticates a signing domain
  through a signature over the message. **DMARC** passes when the RFC 5322 `From` **domain** is
  aligned with an authenticated SPF or DKIM domain.
- All three are **domain-level and message-level** claims. **A DMARC `PASS` does not prove
  ownership or control of any particular local-part mailbox**, and it does not authenticate the
  whole email address. It establishes that the message was authorised by the domain it claims to
  be from.
- **The exact configured correspondent mailbox is a separate comparison**: the digest of the parsed
  `From` addr-spec against the destination registry's `address_digest`. That comparison is what
  narrows a domain-level authentication to the one mailbox this deployment corresponds with.
- **The residual, recorded plainly:** the controlled demo assumes the configured correspondent
  mailbox at that domain is actually operated by the intended property manager. Nothing in SPF,
  DKIM, DMARC, or this ADR establishes that; it is an assumption about a fixed demo counterparty,
  and it is the same class of assumption ADR-026 § Residual risk already records.

Reading `From` rather than `mail.source` is nonetheless the correct choice for a second reason:
DMARC's alignment is defined over `From`, so the verdict gate and this comparison now concern the
same field. The previous pairing authenticated one string and compared another.

`mail.source` is still decoded and still recorded in `ExternalSourceBinding` as transport
provenance. It is no longer an authorization input.

### 6. Agreement 5 reads the matched receipt recipients

Agreement 5 becomes:

> at least one address in **`receipt.recipients`**, normalized, digests to the deployment's
> configured inbound address digest.

`receipt.recipients` is the set the **receipt rule matched** — the delivery authority, and the
field that answers "did this arrive at an address we own?".

**`mail.destination` is not envelope RCPT TO and is not the delivery authority.** It is derived
from the message's own destination headers, and message headers are written by the sender. It is
still decoded and recorded as provenance, and it authorizes nothing.

The **single-recipient refusal is removed from the decode step**: a reply may carry additional
recipients without being refused. The outbound single-recipient rule is untouched, and it was never
this rule.

### 7. Encryption, and a bucket policy that is structurally valid

**Frozen:** SES receipt-rule "message encryption" stays **OFF**. It is client-side encryption
performed with the AWS Encryption SDK, so an object written that way is not readable by the plain
`GetObject` of § 3 — enabling it would hand the attester ciphertext. The object is protected at
rest by the bucket's default SSE-KMS with the private evidence key, exactly like every other
private object. The private bucket keeps Block Public Access in all four forms,
bucket-owner-enforced ownership, `enforce_ssl`, versioning, and every cross-zone restriction.

The problem to solve: the SES receiving service writes without the `x-amz-server-side-encryption`
and `…-aws-kms-key-id` request headers that the bucket's two fail-closed denies require, and those
denies must keep applying in full to every ordinary writer.

**Six separate statements express the exception.** This is a complete JSON policy fragment:
replace the account and key-ARN placeholders with deployment outputs and merge its statements
with the existing bucket policy. It does not replace TLS, public-access, cross-zone, or ingress
anti-substitution restrictions. Normal application write grants remain separately scoped to
their existing paths; this fragment grants no application permission.

The S3Action uses the **direct SES service principal**, with no optional role-based delivery.
AWS sets `aws:PrincipalServiceName` for direct service calls; IAM users, role sessions, and
anonymous callers do not supply that key. Requests using an IAM service role follow the ordinary
writer checks and cannot obtain the SES exception.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowSesReceiptWriteToInboundPrefixOnly",
      "Effect": "Allow",
      "Principal": {
        "Service": "ses.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chorus-private-evidence-demo/ns/DEMO/inbound/*",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "{account}"
        },
        "ArnEquals": {
          "aws:SourceArn": "arn:aws:ses:us-east-1:{account}:receipt-rule-set/chorus-demo-inbound:receipt-rule/chorus-demo-reply"
        }
      }
    },
    {
      "Sid": "DenyNonSesWrongOrMissingEncryption",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chorus-private-evidence-demo/*",
      "Condition": {
        "StringNotEqualsIfExists": {
          "aws:PrincipalServiceName": "ses.amazonaws.com",
          "s3:x-amz-server-side-encryption": "aws:kms"
        }
      }
    },
    {
      "Sid": "DenyNonSesWrongOrMissingKmsKey",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chorus-private-evidence-demo/*",
      "Condition": {
        "StringNotEqualsIfExists": {
          "aws:PrincipalServiceName": "ses.amazonaws.com",
          "s3:x-amz-server-side-encryption-aws-kms-key-id": "{private evidence key ARN}"
        }
      }
    },
    {
      "Sid": "DenySesWrongOrMissingSourceAccount",
      "Effect": "Deny",
      "Principal": {
        "Service": "ses.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chorus-private-evidence-demo/*",
      "Condition": {
        "StringNotEqualsIfExists": {
          "aws:SourceAccount": "{account}"
        }
      }
    },
    {
      "Sid": "DenySesWrongOrMissingReceiptRule",
      "Effect": "Deny",
      "Principal": {
        "Service": "ses.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chorus-private-evidence-demo/*",
      "Condition": {
        "ArnNotEqualsIfExists": {
          "aws:SourceArn": "arn:aws:ses:us-east-1:{account}:receipt-rule-set/chorus-demo-inbound:receipt-rule/chorus-demo-reply"
        }
      }
    },
    {
      "Sid": "DenySesOutsideInboundPrefix",
      "Effect": "Deny",
      "Principal": {
        "Service": "ses.amazonaws.com"
      },
      "Action": "s3:*",
      "NotResource": "arn:aws:s3:::chorus-private-evidence-demo/ns/DEMO/inbound/*"
    }
  ]
}
```

**Boolean proof.** Let `S` mean the AWS-authenticated direct principal is SES, `A` the source
account matches, `R` the full rule ARN matches, `P` the ingress resource matches, `E` the algorithm
header is exactly `aws:kms`, and `K` the key header is the exact private CMK ARN.

- Statement 1 allows only `S AND A AND R AND P`.
- Statements 2 and 3 deny `(NOT S AND NOT E)` and `(NOT S AND NOT K)` respectively. Their
  multi-key AND is intentional: every non-SES writer must satisfy both encryption headers.
  A missing principal-service key or encryption header satisfies `StringNotEqualsIfExists`.
- Statements 4, 5, and 6 **separately** deny `(S AND NOT A)`, `(S AND NOT R)`, and
  `(S AND NOT P)`. Matching the account cannot disable the wrong-rule Deny. These Denies
  override any other Allow and also reject foreign SES calls that supply correct SSE headers.

Thus the only headerless write that can survive these Denies is `S AND A AND R AND P`.
A same-account non-SES service remains `NOT S`, even if it carries matching source fields.
No request-body or event field can set an AWS IAM context key. The ordinary encryption path
cannot be bypassed by forging JSON. The bucket's default SSE-KMS protects the exact SES path;
all other writers still require explicit correct algorithm/key headers.

AWS documents [PrincipalServiceName and its availability](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html#condition-keys-principalservicename)
and [AND evaluation across condition keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-logic-multiple-context-keys-or-values.html).
The private evidence key policy retains its SES grant conditioned on the same expected account
and full receipt-rule ARN. The S3 Denies above remain the mandatory gate for object writes.

**Mandatory truth table**, with all unrelated authorization and TLS requirements satisfied for
allowed cases. Application cases use a normally authorized evidence path, not the ingress prefix
where application writes remain explicitly denied by § 3.

| # | Request | Outcome | Deciding boundary |
|---|---|---|---|
| 1 | Exact SES service + correct account + full rule ARN + ingress prefix; no SSEKMSKeyId header | **ALLOWED** | 1 allows; 2–6 do not deny; bucket-default SSE-KMS applies |
| 2 | Authorized application writer + exact `aws:kms` algorithm + exact SSEKMSKeyId | **ALLOWED** | existing scoped application grant; 2 and 3 do not deny |
| 3 | Authorized application writer + missing SSEKMSKeyId | **DENIED** | 3 explicitly denies |
| 4 | Authorized application writer + wrong SSEKMSKeyId | **DENIED** | 3 explicitly denies |
| 5 | SES + correct account + wrong receipt-rule ARN | **DENIED** | 5 explicitly denies, independently of account match |
| 6 | SES + wrong account + otherwise plausible rule/prefix | **DENIED** | 4 explicitly denies, independently of rule match |
| 7 | Non-SES principal + same account + ingress prefix + missing SSE headers | **DENIED** | 2 and 3 explicitly deny; application ingress-write Deny also remains |
| 8 | Exact SES service/rule/account + wrong prefix | **DENIED** | 6 explicitly denies |
| 9 | Arbitrary principal forges matching JSON/event fields | **DENIED** | § 2's effective publish/invoke boundary rejects it; JSON cannot alter S3 principal/source context or bypass 2–6 |

All nine cases are deployment canaries. Also check missing/wrong algorithm with a correct key,
missing SES source keys, and wrong SES rule/account with correct encryption headers. These
checks must show an explicit Deny where specified, not merely the absence of a matching Allow.

### 8. A transport ingress object is not admitted evidence

SES writes the raw MIME **before** the attester has judged anything. Objects therefore exist under
`ns/DEMO/inbound/` for deliveries later refused for a foreign source, a failed verdict, a missing
correlation, an attachment, an oversize body, or a malformed envelope.

**Physical existence in S3 is not admission.** A rejected ingress object:

- **never** enters case evidence — no `EvidenceItem`, no `EvidenceRoot`, no reference from any case
  partition;
- **never** advances a commitment, an extraction, a case state, or an authorization version;
- **remains transport data only**, accounted for by the private bucket's 30-day lifecycle rule and
  by the reset principal's bounded `ns/DEMO/` prefix deletion, and by nothing else.

Each refusal still emits exactly one `reply.rejected` audit event carrying a closed reason code and
no content, as ADR-026 § 3 froze.

### 9. Proved live, not argued

The decoder gains golden tests over **captured real SES receipt payloads**, not hand-written
dictionaries — inventing the envelope shape is the specific mistake this ADR exists to correct, and
a second round of invented fixtures would repeat it. Canary L in
[the deployment contract](../plans/phase-11-deployment-contract.md) § 15 is the acceptance test: a
real reply, from the verified correspondent identity, to the receiving address, correlating to
exactly one `SENT` execution through one direct locator lookup.

## Alternatives considered

- **Keep `commonHeaders.inReplyTo`.** Rejected: the field is not in the schema, so no client makes
  it appear.
- **S3 action plus a separate SNS action.** Rejected: the notification then describes the SNS
  action and carries no `bucketName`/`objectKey`, leaving the attester nothing to fetch.
- **A Lambda receipt action instead of SNS.** Rejected for the same reason — a later action does
  not carry an earlier action's object locator — and because the SNS topic policy is where the
  `aws:SourceArn` binding to the exact receipt rule is expressed.
- **Plain S3 `ObjectCreated` as the trust event.** Rejected outright: it carries a bucket and a key
  and **no SES receipt verdicts**, which would delete ADR-026 § 2 step 4.
- **Trusting a `TopicArn` read out of the message body.** Rejected: it is ADR-026 § 1's
  `authenticated` boolean wearing a different name.
- **Re-reading the object to obtain headers.** Rejected: two reads of a mutable location are two
  different possible messages, and only one of them was hashed.
- **Comparing both `mail.source` and the `From` mailbox, requiring both.** Rejected: it
  reintroduces the failure for correct relayed mail while adding nothing — an attacker who can
  forge the DMARC-aligned `From` has already defeated the gate.
- **Relaxing or removing the encryption denies to let SES write.** Rejected: that deletes the
  guarantee the denies exist for, on the one bucket that holds raw private evidence.
- **Sub-addressed `Reply-To` routing tokens.** Still rejected, for ADR-026's original reason. It
  remains the first candidate if inbound ever faces more than one correspondent.

## Why chosen

It replaces a transport shape and three decoded fields that were described from documentation with
ones the service actually produces, and it makes the object the attester reasons about a pinned,
hashed, single-read artifact rather than a location that could be re-read. The boundary keeps
failing closed at every one of its refusal codes; what changes is that correct mail now reaches
them, and that the bytes reaching them are provably the bytes SES wrote.

## Consequences

- The receipt rule has one `S3Action` carrying `TopicArn`; the separate SNS receipt action is
  removed. `CHORUS_INBOUND_SOURCE_ARN` holds the **full receipt-rule ARN**.
- `chorus.application.services.inbound_mail` requires `receipt.action.type == "S3"`, validates the
  locator against configuration, performs one bounded pinned read, and consumes that single buffer
  for every downstream step.
- `ExternalSourceBinding` gains the pinned object identity (`ingress_object_version_id`,
  `ingress_object_etag`) and records `mail.source` and `mail.destination` as provenance;
  `sender_address_digest` keeps its name and now holds the digest of the parsed `From` mailbox. No
  stored row is rewritten — no deployment has ingested a reply.
- The decode step reads `mail.headers` (with the pinned-bytes fallback), `mail.commonHeaders.from`,
  and `receipt.recipients`. The single-envelope-recipient refusal is removed;
  `MALFORMED_ENVELOPE` gains the absent/multiple-`From`, wrong-action-type, and bad-locator cases.
- The private bucket policy gains the six statements of § 7 and denies the application principals
  write and delete under `ns/DEMO/inbound/*`; the private KMS key policy gains the matching SES
  grant. Nine independent policy canaries are added.
- Golden tests move to captured real receipt payloads.
- `07-action-ses-and-commitments.md` and `10-security-threat-model.md` need no change: T36 and T37
  are stated over the authenticated-transport boundary, which is unchanged. The correspondent
  residual of § 5 is recorded alongside ADR-026's existing residual risk.
