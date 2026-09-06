# Phase-7 Codex repair regressions

One directory, eleven findings. Every module below fails on the pre-repair implementation for
the exact reason Codex reported, and passes only against the repair.

| Finding | What Codex proved | Regression |
|---|---|---|
| F01 | the real rendered model message omitted required proposal bindings | `test_f01_prompt_binding.py` |
| F02 | punctuation made an unsupported capitalized name vanish from detection | `test_f02_name_punctuation.py` |
| F03 | a view could expire during the model call and still persist a proposal | `test_f03_post_invocation_expiry.py` |
| F04 | a committed-but-unacknowledged apply became a terminal `FAILED` | `test_f04_ambiguous_apply_recovery.py` |
| F05 | request-only and caveat-only citations disappeared from the preview | `test_f05_citation_rendering.py` |
| F06 | `expected_case_version` was absent from HTTP idempotency identity | `test_f06_route_idempotency.py` |
| F07 | `requested_deadline` had no lower bound against `view.generated_at` | `test_f07_deadline_lower_bound.py` |
| F08 | current deployment-configuration freshness was incomplete | `test_f08_deployment_config_freshness.py` |
| F09 | recovery trusted any `SUCCEEDED` invocation record | `test_f09_recovery_provenance.py` |
| F10 | the regenerated preview had no API read path | `test_f10_preview_api.py` |
| F11 | action/execution identities were unapproved UUIDv5 | `test_f11_uuid4_identities.py` |

Three further repairs have no finding number and live here as well:

* `test_bump_table_independent.py` (with `bump_oracle.py`) -- the ADR-020 § 2 bump table,
  transcribed from the document and compared against the machine, importing neither
  `AUTHORIZATION_SENSITIVE_CASE_EDGES` nor `case_edge_bumps_authorization` as its oracle.
* `test_renderer_goldens.py` (with `pinned.py`) -- literal plain-text bytes, literal HTML
  bytes, and a literal preview digest over fully pinned inputs.
* `test_iam_wildcard_hardening.py` -- defence-in-depth negative assertions that a wildcard
  action or resource cannot restore forbidden authority to either principal.

Supporting modules: `pinned.py` (one fully pinned proposal/view pair), `validation_support.py`
(hand-built invocation and result envelopes), `faults.py` (a driver that commits for real and
then loses the acknowledgement), and `bump_oracle.py` (the ADR-020 table by hand).
