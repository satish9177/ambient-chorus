import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import type { FactGrantInput } from "../api/types";
import { DecisionBar } from "../components/mandate/DecisionBar";
import { DestinationPurposeSummary } from "../components/mandate/DestinationPurposeSummary";
import type { DisclosureScope } from "../components/mandate/DisclosureScopeSelect";
import { FactPermissionRow } from "../components/mandate/FactPermissionRow";
import { IdentityPermissionToggle } from "../components/mandate/IdentityPermissionToggle";
import { MandateHistory } from "../components/mandate/MandateHistory";
import { MandatePurposeCard } from "../components/mandate/MandatePurposeCard";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { usePersona } from "../context/PersonaContext";
import { useDecideMandateMutation, useMandateThreadQuery } from "../hooks/useMandate";
import { useSessionQuery } from "../hooks/useSession";
import { RESIDENT_ACTORS } from "../api/session";

type Draft = {
  facts: Record<string, DisclosureScope>;
  identityShareable: boolean;
  identityScope: DisclosureScope;
};

export function MandateThreadPage() {
  const { contributorId } = useParams<{ contributorId: string }>();
  const [searchParams] = useSearchParams();
  const caseId = searchParams.get("case");
  const { actor } = usePersona();
  const sessionQuery = useSessionQuery(actor);

  const threadQuery = useMandateThreadQuery(contributorId ?? null, caseId, actor);
  const [isAdjusting, setIsAdjusting] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);

  const thread = threadQuery.data;
  const decideMutation = useDecideMandateMutation(
    caseId ?? "",
    thread?.mandate_id ?? "",
    contributorId ?? "",
    actor,
  );

  const canDecide =
    RESIDENT_ACTORS.includes(actor) && sessionQuery.data?.contributor_id === contributorId;

  if (!contributorId || !caseId) {
    return <ErrorBanner error={new Error("This mandate link is missing a contributor or case.")} />;
  }

  function startAdjust() {
    if (!thread) return;
    setDraft({
      facts: Object.fromEntries(
        thread.fact_permissions.map((f) => [f.fact_id, f.proposed_scope as DisclosureScope]),
      ),
      identityShareable: thread.identity_permission.externally_shareable,
      identityScope: thread.identity_permission.max_scope as DisclosureScope,
    });
    setIsAdjusting(true);
  }

  function cancelAdjust() {
    setIsAdjusting(false);
    setDraft(null);
  }

  function submit(decision: "APPROVE" | "ADJUST" | "REFUSE" | "REVOKE") {
    if (!thread) return;
    const factGrants: FactGrantInput[] =
      decision === "APPROVE"
        ? thread.fact_permissions.map((f) => ({
            fact_id: f.fact_id,
            max_scope: f.proposed_scope as DisclosureScope,
            allow_safe_transformation: f.allow_safe_transformation,
          }))
        : decision === "ADJUST" && draft
          ? thread.fact_permissions.map((f) => ({
              fact_id: f.fact_id,
              max_scope: draft.facts[f.fact_id] ?? (f.proposed_scope as DisclosureScope),
              allow_safe_transformation: f.allow_safe_transformation,
            }))
          : [];

    const identityGrant =
      decision === "ADJUST" && draft
        ? { externally_shareable: draft.identityShareable, max_scope: draft.identityScope }
        : {
            externally_shareable: thread.identity_permission.externally_shareable,
            max_scope: thread.identity_permission.max_scope as DisclosureScope,
          };

    decideMutation.mutate(
      {
        expected_version: thread.current_version,
        decision,
        fact_grants: factGrants,
        identity_grant: identityGrant,
      },
      {
        onSuccess: () => {
          setIsAdjusting(false);
          setDraft(null);
        },
      },
    );
  }

  return (
    <section aria-labelledby="mandate-heading">
      <h2 id="mandate-heading">Private mandate thread</h2>
      <p>
        <Link to={`/cases/${caseId}`}>Return to case</Link>
      </p>

      {threadQuery.isPending && <p>Loading mandate…</p>}
      {threadQuery.isError && <ErrorBanner error={threadQuery.error} />}

      {thread && (
        <>
          <MandatePurposeCard status={thread.status} />
          <DestinationPurposeSummary
            destinations={thread.allowed_destination_ids}
            purposes={thread.allowed_purposes}
            expiresAt={thread.expires_at}
          />

          <h3>What you reported</h3>
          {thread.fact_permissions.map((fact) => (
            <FactPermissionRow
              key={fact.fact_id}
              fact={fact}
              editable={isAdjusting}
              value={
                isAdjusting && draft
                  ? (draft.facts[fact.fact_id] ?? (fact.proposed_scope as DisclosureScope))
                  : (fact.proposed_scope as DisclosureScope)
              }
              onChange={(next) =>
                setDraft((prev) =>
                  prev ? { ...prev, facts: { ...prev.facts, [fact.fact_id]: next } } : prev,
                )
              }
            />
          ))}

          <IdentityPermissionToggle
            identity={thread.identity_permission}
            editable={isAdjusting}
            externallyShareable={
              isAdjusting && draft
                ? draft.identityShareable
                : thread.identity_permission.externally_shareable
            }
            scope={
              isAdjusting && draft
                ? draft.identityScope
                : (thread.identity_permission.max_scope as DisclosureScope)
            }
            onChangeShareable={(v) =>
              setDraft((prev) => (prev ? { ...prev, identityShareable: v } : prev))
            }
            onChangeScope={(v) =>
              setDraft((prev) => (prev ? { ...prev, identityScope: v } : prev))
            }
          />

          <DecisionBar
            canDecide={canDecide}
            status={thread.status}
            isAdjusting={isAdjusting}
            pending={decideMutation.isPending}
            onApprove={() => submit("APPROVE")}
            onStartAdjust={startAdjust}
            onSubmitAdjust={() => submit("ADJUST")}
            onCancelAdjust={cancelAdjust}
            onRefuse={() => submit("REFUSE")}
            onRevoke={() => submit("REVOKE")}
          />

          {decideMutation.isError && <ErrorBanner error={decideMutation.error} />}

          <MandateHistory history={thread.history} />
        </>
      )}
    </section>
  );
}
