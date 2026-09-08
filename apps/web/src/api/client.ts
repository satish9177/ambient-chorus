/**
 * The one hand-written API file (11-frontend-and-demo.md § Generated API types). It declares
 * no request/response shapes of its own beyond the Problem Details envelope, which the
 * generated schema does not model accurately: FastAPI documents its default validation
 * response there, but `chorus_api.problem_details` installs a custom handler that answers
 * every error, 422 included, with the RFC 9457 shape frozen in 08-api-design.md. Every other
 * type is imported from `schema.d.ts` through `./types`.
 */
import type { DemoActor } from "./session";
import { getSessionToken } from "./session";

const API_BASE = "/v1";

export type ProblemDetails = {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  instance?: string;
  correlation_id: string;
  retryable: boolean;
  errors: { code: string; path: string; category: string }[];
};

export class ApiError extends Error {
  readonly problem: ProblemDetails;
  readonly status: number;

  constructor(problem: ProblemDetails) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
    this.problem = problem;
    this.status = problem.status;
  }
}

export class NetworkError extends Error {
  constructor() {
    super("The network is unavailable. Check your connection and try again.");
    this.name = "NetworkError";
  }
}

function isProblemDetails(value: unknown): value is ProblemDetails {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "code" in value &&
    "title" in value
  );
}

export type RequestQuery = Record<string, string | number | boolean | undefined>;

export type RequestOptions = {
  actor: DemoActor;
  query?: RequestQuery;
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
};

function buildUrl(path: string, query?: RequestQuery): string {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return `${url.pathname}${url.search}`;
}

function headersFor(opts: RequestOptions, hasBody: boolean): HeadersInit {
  const headers: Record<string, string> = {
    "X-Chorus-Demo-Actor": opts.actor,
    "X-Correlation-Id": crypto.randomUUID(),
    Authorization: `Bearer ${getSessionToken()}`,
  };
  if (hasBody) headers["Content-Type"] = "application/json";
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  return headers;
}

async function parseBody<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError({
      type: "urn:chorus:error:malformed-response",
      title: "The server returned a response that could not be parsed.",
      status: response.status,
      code: "MALFORMED_RESPONSE",
      detail: "Refresh and try again.",
      correlation_id: "",
      retryable: false,
      errors: [],
    });
  }
}

async function send<T>(
  method: "GET" | "POST",
  path: string,
  opts: RequestOptions,
): Promise<T> {
  const hasBody = opts.body !== undefined;
  const init: RequestInit = {
    method,
    headers: headersFor(opts, hasBody),
    cache: "no-store",
    ...(hasBody ? { body: JSON.stringify(opts.body) } : {}),
    ...(opts.signal ? { signal: opts.signal } : {}),
  };
  const url = buildUrl(path, opts.query);

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    if (method === "GET") {
      try {
        response = await fetch(url, init);
      } catch {
        throw new NetworkError();
      }
    } else {
      throw new NetworkError();
    }
  }

  if (!response.ok) {
    const body = await parseBody<unknown>(response);
    if (isProblemDetails(body)) throw new ApiError(body);
    throw new ApiError({
      type: "urn:chorus:error:unknown",
      title: `Request failed with status ${response.status}`,
      status: response.status,
      code: "UNKNOWN_ERROR",
      detail: "An unexpected error occurred.",
      correlation_id: response.headers.get("X-Correlation-Id") ?? "",
      retryable: false,
      errors: [],
    });
  }
  return parseBody<T>(response);
}

export function apiGet<T>(path: string, opts: RequestOptions): Promise<T> {
  return send<T>("GET", path, opts);
}

export function apiPost<T>(path: string, opts: RequestOptions): Promise<T> {
  return send<T>("POST", path, opts);
}

/** One UUID per user intent, stable until the intent completes or is deliberately restarted. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

const STALE_CONFLICT_CODES = new Set([
  "PERSISTENCE_CONFLICT",
  "STALE_AUTHORIZATION",
  "IDEMPOTENCY_CONFLICT",
  "EXECUTION_NOT_DRAFT",
]);

/**
 * The closed reason codes a stale/binding conflict carries when the backend answers `422`
 * `VALIDATION_ERROR` rather than a `409` (08-api-design.md § Propose, approve, execute). A
 * stale approve holds an old `expected_execution_version`, `proposal_hash`, `preview_hash`,
 * `view_hash`, or authorization epoch, and the use case refuses it before staging anything with
 * one of these `ApprovalDenial` / send pre-send reason codes. The domain problem-details
 * handler surfaces that code in `problem.errors` (additively — an ordinary malformed body still
 * carries none of these), which is the strongest structured signal already available to tell a
 * stale binding apart from ordinary invalid input.
 */
const STALE_BINDING_REASON_CODES = new Set([
  // approve_action.py ApprovalDenial
  "NO_CURRENT_PROPOSAL",
  "PROPOSAL_NOT_CURRENT",
  "POINTER_NOT_DRAFT",
  "PROPOSAL_HASH_MISMATCH",
  "PREVIEW_HASH_MISMATCH",
  "VIEW_HASH_MISMATCH",
  "EXECUTION_NOT_DRAFT",
  "EXECUTION_NOT_CURRENT",
  "EXECUTION_VERSION_MISMATCH",
  "CASE_NOT_ACTION_PROPOSED",
  "STALE_AUTHORIZATION",
  "VIEW_EXPIRED",
  "DEPLOYMENT_CONFIGURATION_MOVED",
  "PREVIEW_BINDING_MOVED",
]);

/** Every reason code the domain error carried, whether reported as strings or `{code}` objects. */
function reasonCodesOf(problem: ProblemDetails): string[] {
  const raw = (problem as { errors?: unknown }).errors;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry) => (typeof entry === "string" ? entry : (entry as { code?: unknown })?.code))
    .filter((code): code is string => typeof code === "string");
}

/**
 * True for an OCC/hash/version conflict a mutation must fail closed on (P2-5): the client's
 * view of the world was stale, so the fix is to refetch and require a fresh deliberate click,
 * never to resubmit the same body automatically — a same-key retry of a *different* request is
 * exactly the case 08-api-design.md's idempotency rules refuse.
 *
 * A `409` and the explicit conflict codes are unambiguous. A `422 VALIDATION_ERROR` is only
 * treated as stale when it carries one of the structured stale-binding reason codes above — an
 * ordinary malformed-input `422` (a bad enum, a missing field) carries none of them and is not
 * misclassified.
 */
export function isStaleConflict(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status === 409 || STALE_CONFLICT_CODES.has(error.problem.code)) return true;
  if (error.status === 422 && error.problem.code === "VALIDATION_ERROR") {
    return reasonCodesOf(error.problem).some((code) => STALE_BINDING_REASON_CODES.has(code));
  }
  return false;
}

const OPERATION_CONFLICT_CODES = new Set([
  ...STALE_CONFLICT_CODES,
  "CONFLICT",
  "SUPERSEDED_PROPOSAL",
]);

/**
 * True when an async operation that was accepted (202) terminally `FAILED` with a code that
 * means the caller's execution intent is now stale — the replay table refused it (`CONFLICT`),
 * the authorization epoch moved (`STALE_AUTHORIZATION`), or a write conflicted
 * (`PERSISTENCE_CONFLICT`). The caller discards the intent and refetches the case; it never
 * re-executes automatically. An arbitrary model/business failure (`INTERNAL_ERROR`, an SES
 * rejection code) is not in this set and is surfaced as a plain failure instead.
 */
export function isConflictOperationFailure(errorCode: string | null | undefined): boolean {
  return typeof errorCode === "string" && OPERATION_CONFLICT_CODES.has(errorCode);
}
