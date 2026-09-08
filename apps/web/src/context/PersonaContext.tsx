import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { getStoredActor, setStoredActor, type DemoActor } from "../api/session";

type PersonaContextValue = {
  actor: DemoActor;
  setActor: (actor: DemoActor) => void;
};

const PersonaContext = createContext<PersonaContextValue | null>(null);

export function PersonaProvider({ children }: { children: ReactNode }) {
  const [actor, setActorState] = useState<DemoActor>(() => getStoredActor());

  const value = useMemo<PersonaContextValue>(
    () => ({
      actor,
      setActor: (next) => {
        setStoredActor(next);
        setActorState(next);
      },
    }),
    [actor],
  );

  return <PersonaContext.Provider value={value}>{children}</PersonaContext.Provider>;
}

export function usePersona(): PersonaContextValue {
  const context = useContext(PersonaContext);
  if (!context) throw new Error("usePersona must be used within a PersonaProvider");
  return context;
}
