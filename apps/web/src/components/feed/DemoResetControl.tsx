import { useState } from "react";

import { ApiError } from "../../api/client";
import { usePersona } from "../../context/PersonaContext";
import { useFeedInvalidation, useResetMutation, useStartDiscoveryMutation } from "../../hooks/useFeed";
import { ErrorBanner } from "../shared/ErrorBanner";
import { OperationProgress } from "../shared/OperationProgress";
import styles from "./DemoResetControl.module.css";

export function DemoResetControl({
  communityId,
  hasSignal,
}: {
  communityId: string | null;
  hasSignal: boolean;
}) {
  const { actor } = usePersona();
  const reset = useResetMutation(actor);
  const discovery = useStartDiscoveryMutation(communityId, actor);
  const invalidateFeed = useFeedInvalidation(communityId);
  const [discoveryOperationId, setDiscoveryOperationId] = useState<string | null>(null);

  const resetBlocked =
    reset.error instanceof ApiError &&
    (reset.error.status === 409 || reset.error.status === 422);

  return (
    <div className={styles.panel} aria-label="Demo controls">
      <button
        type="button"
        className={styles.button}
        onClick={() => reset.mutate()}
        disabled={reset.isPending}
      >
        {reset.isPending ? "Resetting…" : "Reset demo"}
      </button>

      <button
        type="button"
        className={styles.button}
        data-variant="primary"
        onClick={() => {
          discovery.mutate(undefined, {
            onSuccess: (result) => setDiscoveryOperationId(result.operation.operation_id),
          });
        }}
        disabled={!communityId || discovery.isPending || hasSignal}
      >
        {discovery.isPending ? "Starting…" : hasSignal ? "Pattern detected" : "Detect pattern"}
      </button>

      {reset.isSuccess && !reset.isPending && (
        <span className={styles.status} data-tone="success" role="status">
          Reset complete — {reset.data.counts.messages} messages seeded.
        </span>
      )}

      {discoveryOperationId && (
        <OperationProgress
          operationId={discoveryOperationId}
          actor={actor}
          label="Monitor"
          onSucceeded={invalidateFeed}
        />
      )}

      {reset.isError && (
        <ErrorBanner
          error={reset.error}
          action={
            resetBlocked && (
              <span>
                An execution may still be sending or its outcome may be unknown. Reset is
                refused until that is resolved — this is a safety guard, not a bug, and it will
                not be retried automatically.
              </span>
            )
          }
        />
      )}
      {discovery.isError && <ErrorBanner error={discovery.error} />}
    </div>
  );
}
