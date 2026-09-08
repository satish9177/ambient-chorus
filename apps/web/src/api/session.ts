/**
 * The demo persona registry and the session-scoped access token.
 *
 * The token lives in `sessionStorage`, never `localStorage`, a query string, or a log line
 * (11-frontend-and-demo.md § Frontend decisions). Phase 10's local composition does not
 * validate it, but the header is sent on every request regardless, because that is the frozen
 * deployed contract and nothing about running locally should make the client behave
 * differently than it will against a real deployment.
 */

export const DEMO_ACTORS = [
  "presenter_admin",
  "resident_a",
  "resident_b",
  "resident_c",
  "resident_d",
  "case_approver",
] as const;

export type DemoActor = (typeof DEMO_ACTORS)[number];

export const RESIDENT_ACTORS: readonly DemoActor[] = [
  "resident_a",
  "resident_b",
  "resident_c",
  "resident_d",
];

export const ACTOR_LABELS: Record<DemoActor, string> = {
  presenter_admin: "Presenter",
  resident_a: "Resident A",
  resident_b: "Resident B",
  resident_c: "Resident C",
  resident_d: "Resident D",
  case_approver: "Case approver",
};

const TOKEN_STORAGE_KEY = "chorus.demo.token";
const ACTOR_STORAGE_KEY = "chorus.demo.actor";
const DEFAULT_TOKEN = "demo-local-token";

export function getSessionToken(): string {
  try {
    const existing = window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (existing) return existing;
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, DEFAULT_TOKEN);
    return DEFAULT_TOKEN;
  } catch {
    return DEFAULT_TOKEN;
  }
}

export function getStoredActor(): DemoActor {
  try {
    const stored = window.sessionStorage.getItem(ACTOR_STORAGE_KEY);
    if (stored && (DEMO_ACTORS as readonly string[]).includes(stored)) {
      return stored as DemoActor;
    }
  } catch {
    // sessionStorage unavailable; fall through to the default persona.
  }
  return "presenter_admin";
}

export function setStoredActor(actor: DemoActor): void {
  try {
    window.sessionStorage.setItem(ACTOR_STORAGE_KEY, actor);
  } catch {
    // Best effort only: an in-memory context still holds the current persona for this tab.
  }
}
