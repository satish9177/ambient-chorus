import type { ReactNode } from "react";

import styles from "./Badge.module.css";

export type BadgeTone = "neutral" | "private" | "shareable" | "danger" | "success";

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span className={styles.badge} data-tone={tone}>
      {children}
    </span>
  );
}
