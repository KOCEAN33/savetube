export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export function loginUrl(): string {
  const next = window.location.pathname + window.location.search
  return `/auth/login?next=${encodeURIComponent(next)}`
}

// An array repeats the parameter, once per value.
type Query = Record<string, string | number | string[] | undefined>

// Parallel requests that all get 401 must start one login, not one each: every
// /auth/login rewrites the session cookie with a new state and PKCE verifier,
// so concurrent logins break each other's callback.
let redirectingToLogin = false

// FastAPI puts a message in `detail`: a string from the app, or a list of
// validation errors from pydantic.
async function errorMessage(res: Response, path: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === "string") return body.detail
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${d.loc?.slice(1).join(".")}: ${d.msg}`)
        .join("; ")
    }
  } catch {
    // Not JSON: fall through to the status line.
  }
  return `${res.status} ${res.statusText} for ${path}`
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" }
  if (method !== "GET") {
    // The backend refuses a write without it: a cross-origin page cannot set
    // a custom header without a CORS preflight.
    headers["X-Requested-With"] = "savetube"
  }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401) {
    if (!redirectingToLogin) {
      redirectingToLogin = true
      window.location.assign(loginUrl())
    }
    throw new ApiError(401, "Not signed in")
  }
  if (!res.ok) {
    throw new ApiError(res.status, await errorMessage(res, path))
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function get<T>(path: string, query: Query = {}): Promise<T> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (Array.isArray(value)) for (const v of value) params.append(key, v)
    else if (value !== undefined && value !== "") params.set(key, String(value))
  }
  const qs = params.toString()
  return request<T>("GET", qs ? `${path}?${qs}` : path)
}
