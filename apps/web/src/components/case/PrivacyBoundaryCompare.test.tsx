import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Investigation } from "../../api/private";
import type { ShareableCaseView } from "../../api/shareable";
import { PrivateInvestigationPanel } from "../private/PrivateInvestigationPanel";
import { ShareableExternalViewPanel } from "../shareable/ShareableExternalViewPanel";
import { PrivacyBoundaryCompare } from "./PrivacyBoundaryCompare";

const SENTINEL = "Leela";

const investigation: Investigation = {
  case: {
    case_id: "c1",
    title: "Recurring lift failures",
    state: "INVESTIGATING",
    version: 3,
    authorization_version: 1,
    corroboration_source_count: 2,
  },
  reports: [],
  facts: [
    {
      fact_id: "f1",
      fact_type: "IDENTITY_ATTRIBUTE",
      sensitivity: "SENSITIVE",
      value_preview: `Resident B's mother ${SENTINEL} has asthma`,
      evidence_status: "REPORTED",
      status: "ACTIVE",
      contributor_id: "ct1",
      evidence_ids: [],
      source_message_ids: [],
      version: 1,
    },
  ],
  assessment: null,
  compile: null,
};

const shareableView: ShareableCaseView = {
  schema_version: "shareable-case-view/v1",
  view_id: "v1",
  case_id: "c1",
  community_public_label: "Example Community Building",
  case_version: 3,
  authorization_version: 1,
  policy_version: "policy/v1",
  compiler_version: "compiler/v1",
  destination: {
    destination_id: "property_manager:demo",
    kind: "PROPERTY_MANAGER",
    registry_version: 1,
    routing_token: "rt1",
    display_label: "Property manager",
  },
  purpose: "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
  generated_at: "2030-01-14T09:00:00.000000Z",
  expires_at: "2030-01-21T09:00:00.000000Z",
  mandate_version_set: [],
  authorization_snapshot_hash: "sha256:00",
  shareable_facts: [
    {
      export_fact_id: "e1",
      fact_type: "INCIDENT_OCCURRENCE",
      safe_text: "An elevator incident was reported.",
      effective_scope: "ANONYMOUS_CASE",
      evidence_status: "REPORTED",
      contributor_count: 2,
      transformation: "ANONYMIZED",
      transformation_rule_id: "redact/v1",
      safe_evidence_ref_ids: [],
      content_hash: "sha256:01",
    },
  ],
  safe_evidence_refs: [],
  audit_refs: [],
  view_hash: "sha256:02",
};

describe("PrivacyBoundaryCompare", () => {
  it("keeps the private sentinel out of the shareable DOM subtree", () => {
    const { container } = render(
      <PrivacyBoundaryCompare
        left={<PrivateInvestigationPanel investigation={investigation} />}
        right={<ShareableExternalViewPanel view={shareableView} privacyCounts={null} />}
      />,
    );

    // Sanity check: the private panel really does render the sentinel somewhere.
    expect(screen.getByText(new RegExp(SENTINEL))).toBeInTheDocument();

    const shareablePanel = container.querySelector('[aria-labelledby="shareable-view-heading"]');
    expect(shareablePanel).not.toBeNull();
    expect(shareablePanel?.textContent ?? "").not.toContain(SENTINEL);
  });

  it("states the private -> compiler -> shareable relationship as real, non-hidden heading text (P3-11)", () => {
    render(
      <PrivacyBoundaryCompare
        left={<PrivateInvestigationPanel investigation={investigation} />}
        right={<ShareableExternalViewPanel view={shareableView} privacyCounts={null} />}
      />,
    );

    const heading = document.getElementById("privacy-boundary-heading");
    expect(heading).not.toBeNull();
    expect(heading).not.toHaveAttribute("aria-hidden");
    expect(heading).toHaveTextContent("Private");
    expect(heading).toHaveTextContent("Deterministic privacy compiler");
    expect(heading).toHaveTextContent("Shareable");
  });
});
