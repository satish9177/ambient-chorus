import type { ReactNode } from "react";

import { ApiError, NetworkError } from "../../api/client";
import styles from "./ErrorBanner.module.css";

const FRIENDLY_TITLES: Record<string, string> = {
  STALE_AUTHORIZATION: "This view has changed since you last saw it",
  IDEMPOTENCY_CONFLICT: "That request could not be repeated as sent",
  POLICY_DENIED: "The privacy compiler denied this request",
  EXECUTION_NOT_DRAFT: "This action has already moved on",
  VALIDATION_ERROR: "That request was not accepted",
};

export function describeError(error: unknown): { title: string; detail: string } {
  if (error instanceof ApiError) {
    return {
      title: FRIENDLY_TITLES[error.problem.code] ?? error.problem.title,
      detail: error.problem.detail || "Refresh to see the current state and try again.",
    };
  }
  if (error instanceof NetworkError) {
    return { title: "Network unavailable", detail: error.message };
  }
  return { title: "Something went wrong", detail: "Refresh to see the current state." };
}

export function ErrorBanner({
  error,
  tone = "danger",
  action,
}: {
  error: unknown;
  tone?: "danger" | "neutral";
  action?: ReactNode;
}) {
  const { title, detail } = describeError(error);
  return (
    <div className={styles.banner} data-tone={tone} role="alert">
      <div>
        <p className={styles.title}>{title}</p>
        <p className={styles.detail}>{detail}</p>
        {action && <div className={styles.actions}>{action}</div>}
      </div>
    </div>
  );
}
