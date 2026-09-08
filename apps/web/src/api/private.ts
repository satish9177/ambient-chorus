/**
 * The private-zone wire types. Only `components/private/**` may import this module — see
 * `no-restricted-imports` in `eslint.config.js`, which forbids it under
 * `components/shareable/**`. `api/boundary.test.ts` asserts this module and `shareable.ts`
 * export disjoint names.
 *
 * Every type below carries, or is nested under a type that carries, a fact's raw value, a
 * private case title, an exclusion reason, or another presenter-only detail. None may reach a
 * shareable-zone component.
 */
import type { Schemas } from "./types";

export type Investigation = Schemas["InvestigationResponse"];
export type InvestigationCase = Schemas["InvestigationCaseResponse"];
export type PrivateFact = Schemas["FactResponse"];
export type Report = Schemas["ReportResponse"];
export type Assessment = Schemas["AssessmentResponse"];
export type Contradiction = Schemas["ContradictionResponse"];
export type Finding = Schemas["FindingResponse"];
export type AlternativeExplanation = Schemas["AlternativeExplanationResponse"];
export type PrivateCompileExplanation = Schemas["CompileExplanationResponse"];
export type PrivateCompileFact = Schemas["CompileFactResponse"];
export type PrivateCompileEvidence = Schemas["CompileEvidenceResponse"];
export type ExcludedFact = Schemas["ExcludedFactBody"];
export type PrivateCaseHeader = Schemas["CaseHeaderResponse"];
export type PrivateEvidenceRow = Schemas["EvidenceSummaryRowResponse"];
