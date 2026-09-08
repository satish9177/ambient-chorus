/**
 * The shareable-zone wire types. `components/shareable/**` may import only from this module
 * (enforced by `no-restricted-imports` in `eslint.config.js`) — never from `api/private`.
 * `api/boundary.test.ts` asserts this module and `private.ts` export disjoint names.
 *
 * Every type below is either the compiler's own external-safe artifact or the safe projection
 * of the current action/execution. Nothing here carries a fact's raw value or a private ID
 * paired with a denial reason.
 */
import type { Schemas } from "./types";

export type ShareableCaseView = Schemas["ShareableCaseViewBody"];
export type ShareableFact = Schemas["ShareableFactBody"];
export type ShareableEvidenceRef = Schemas["ShareableEvidenceRefBody"];
export type SafeDestination = Schemas["SafeDestinationBody"];
export type MandateVersionRef = Schemas["MandateVersionRefBody"];

export type PrivacyCounts = Schemas["PrivacyCountsResponse"];

export type SafeCurrentAction = Schemas["CurrentActionResponse"];
export type SafeClaim = Schemas["ClaimProjection"];
export type SafeCaveat = Schemas["CaveatProjection"];
export type SafePreview = Schemas["RenderedPreviewProjection"];
export type SafeExecution = Schemas["ExecutionProjection"];
