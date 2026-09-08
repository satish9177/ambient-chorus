/**
 * Generic helpers over the generated `schema.d.ts`, plus one alias per wire shape this app
 * touches. Every type here is derived from `components`/`operations` — none is redeclared.
 */
import type { components, operations } from "./schema";

export type Schemas = components["schemas"];

export type OpBody<Op extends keyof operations> = operations[Op] extends {
  requestBody: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type OpResponse<
  Op extends keyof operations,
  Status extends number = 200,
> = operations[Op]["responses"] extends Record<string, unknown>
  ? Status extends keyof operations[Op]["responses"]
    ? operations[Op]["responses"][Status] extends { content: { "application/json": infer R } }
      ? R
      : never
    : never
  : never;

export type OpQuery<Op extends keyof operations> = operations[Op] extends {
  parameters: { query: infer Q };
}
  ? Q
  : Record<string, never>;

// -- Entity aliases used across more than one surface --------------------------------------

export type FeedItem = Schemas["FeedItemResponse"];
export type ChorusSignal = Schemas["ChorusSignalResponse"];
export type AttachmentThumbnail = Schemas["AttachmentThumbnailResponse"];

export type SessionInfo = Schemas["SessionResponse"];
export type ResetResult = Schemas["ResetResponse"];

export type OperationStatus = Schemas["OperationResponse"];

export type MandateThread = Schemas["MandateThreadResponse"];
export type FactPermission = Schemas["FactPermissionResponse"];
export type IdentityPermission = Schemas["IdentityPermissionResponse"];
export type MandateVersion = Schemas["MandateVersionResponse"];
export type ProposedMandate = Schemas["ProposedMandateResponse"];
export type FactGrantInput = Schemas["FactGrantRequest"];
export type IdentityGrantInput = Schemas["IdentityGrantRequest"];
export type MandateDecisionKind = FactGrantInput["max_scope"];

export type CaseSurface = Schemas["CaseSurfaceResponse"];
export type CaseHeader = Schemas["CaseHeaderResponse"];
export type EvidenceSummaryRow = Schemas["EvidenceSummaryRowResponse"];
export type PrivacyCounts = Schemas["PrivacyCountsResponse"];
export type CommitmentSafe = Schemas["CommitmentSafeResponse"];
export type CurrentAction = Schemas["CurrentActionResponse"];
export type ExecutionProjection = Schemas["ExecutionProjection"];
export type RenderedPreview = Schemas["RenderedPreviewProjection"];

export type AuditEvent = Schemas["AuditEventResponse"];
export type AuditPage = Schemas["AuditPageResponse"];

export type VerificationView = Schemas["VerificationView"];
export type DemoClockView = Schemas["DemoClockView"];

export type CaseState = CaseHeader["state"];
