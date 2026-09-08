import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * Remembers only the case id the presenter has already opened this session, purely so a
 * resident persona — who cannot read the ambient feed or the case surface themselves
 * (`require_presenter`/`require_case_reader`) — can still reach their own mandate thread
 * through a real in-app link rather than a URL a test or a person has to construct by hand
 * (P2-3). It never grants a read; `/mandates/:contributorId` still authorizes independently.
 *
 * It deliberately holds **no actor identity**. There is no `readerActor`, no remembered
 * presenter, no "last authorized reader": every HTTP request the case page makes is issued as
 * the *currently active* persona, so a persona switch changes what the backend returns rather
 * than being papered over by a retained privileged identity (P1-1). `caseId` is navigation
 * state (already public in the URL); an actor is authorization state and must not be cached
 * here.
 */
type DemoCaseContextValue = {
  caseId: string | null;
  setCaseId: (caseId: string) => void;
};

const DemoCaseContext = createContext<DemoCaseContextValue | null>(null);

export function DemoCaseProvider({ children }: { children: ReactNode }) {
  const [caseId, setCaseIdState] = useState<string | null>(null);

  const value = useMemo<DemoCaseContextValue>(
    () => ({ caseId, setCaseId: setCaseIdState }),
    [caseId],
  );

  return <DemoCaseContext.Provider value={value}>{children}</DemoCaseContext.Provider>;
}

export function useDemoCase(): DemoCaseContextValue {
  const context = useContext(DemoCaseContext);
  if (!context) throw new Error("useDemoCase must be used within a DemoCaseProvider");
  return context;
}
