export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// The callback sends a failed login back with this parameter.
export const AUTH_ERROR_PARAM = "auth_error"

export function loginUrl(): string {
  const search = new URLSearchParams(window.location.search)
  search.delete(AUTH_ERROR_PARAM)
  const qs = search.toString()
  const next = window.location.pathname + (qs ? `?${qs}` : "")
  return `/auth/login?next=${encodeURIComponent(next)}`
}

// An array repeats the parameter, once per value.
type Query = Record<string, string | number | boolean | string[] | undefined>

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

async function send(method: string, path: string, body?: unknown): Promise<Response> {
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
  // No redirect here: the login wall starts a login on the user's click. Every
  // /auth/login rewrites the session cookie with a new state and PKCE verifier,
  // so logins that start on their own, one per tab, break each other's callback.
  if (res.status === 401) throw new ApiError(401, "Not signed in")
  if (!res.ok) {
    throw new ApiError(res.status, await errorMessage(res, path))
  }
  return res
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await send(method, path, body)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function withQuery(path: string, query: Query): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (Array.isArray(value)) for (const v of value) params.append(key, v)
    else if (value !== undefined && value !== "") params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

export function get<T>(path: string, query: Query = {}): Promise<T> {
  return request<T>("GET", withQuery(path, query))
}

// A list endpoint that counts every match in X-Total-Count and returns one page as the body.
export async function getPage<T>(
  path: string,
  query: Query = {},
): Promise<{ items: T[]; total: number }> {
  const res = await send("GET", withQuery(path, query))
  return { items: (await res.json()) as T[], total: Number(res.headers.get("X-Total-Count")) }
}
