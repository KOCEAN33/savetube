export type VideoType = "videos" | "shorts" | "lives" | "members"
export type JobKind = "archive" | "record"
export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "missed"
  | "cancelled"
export type MissReason = "dvr_off" | "ended_before_detection" | "recorder_failed"

export interface Me {
  sub: string
  name: string | null
  email: string | null
  groups: string[]
}

export interface StatsSummary {
  total_bytes: number
  video_count: number
  duration_seconds: number
  free_bytes: number | null
  by_type: { type: VideoType; bytes: number; count: number }[]
  by_channel: { channel_id: string; name: string; bytes: number; count: number }[]
}

export interface DailyStat {
  day: string
  bytes: number
  count: number
}

export interface Channel {
  id: string
  handle: string | null
  name: string
  dir_name: string
  video_count: number
  bytes: number
  include_words: string[]
  exclude_words: string[]
}

export interface Source {
  id: number
  url: string
  kind: "tab" | "playlist" | "playlists"
  channel_id: string | null
  parent_id: number | null
  // Child playlist URLs a `playlists` parent must not re-add. Empty on any other source.
  ignored_urls: string[]
  // Off: polls list the source but download nothing.
  archiving: boolean
  include_words: string[]
  exclude_words: string[]
  other_channels: boolean
  enabled: boolean
  poll_interval_minutes: number
  rule_id: number
  last_polled_at: string | null
  last_error: string | null
}

export interface Rule {
  id: number
  name: string
  types: string[]
  date_after: string | null
  format: string
  use_cookies: boolean
  watch_lives: boolean
}

export interface Job {
  id: number
  kind: JobKind
  state: JobState
  video_id: string | null
  attempts: number
  miss_reason: MissReason | null
  scheduled_at: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  // A queued archive job waiting for the download window or a free slot.
  deferred_until: string | null
}

export interface Video {
  id: string
  channel_id: string
  type: string
  title: string
  upload_date: string | null
  duration: number | null
  size_bytes: number | null
  state: string
  completed_at: string | null
}

export interface VideoDetail extends Video {
  info: Record<string, unknown>
}

export interface VideoPage {
  total: number
  items: Video[]
}

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

type Query = Record<string, string | number | undefined>

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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
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

function get<T>(path: string, query: Query = {}): Promise<T> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") params.set(key, String(value))
  }
  const qs = params.toString()
  return request<T>("GET", qs ? `${path}?${qs}` : path)
}

export type Tab = "videos" | "shorts" | "streams" | "playlists"

export interface SourceIn {
  url: string
  tabs: Tab[]
  rule_id: number
  poll_interval_minutes: number
  include_words: string[]
  exclude_words: string[]
  other_channels: boolean
}

export type SourcePatch = Partial<
  Pick<
    Source,
    | "enabled"
    | "rule_id"
    | "poll_interval_minutes"
    | "include_words"
    | "exclude_words"
    | "other_channels"
    | "ignored_urls"
    | "archiving"
  >
>

export interface ChannelPlaylist {
  id: string
  title: string
  url: string
  source_id: number | null
}

export interface TitleWord {
  word: string
  count: number
}

export interface TitleWords {
  include_words: string[]
  exclude_words: string[]
}

export interface TitlePreview {
  total: number
  kept: number
  titles: { video_id: string; title: string; kept: boolean }[]
}

export type RuleIn = Omit<Rule, "id">

export interface DownloadSettingsIn {
  max_concurrent: number
  rate_limit_bytes: number | null
  window_start: string | null
  window_end: string | null
}

export interface DownloadSettings extends DownloadSettingsIn {
  timezone: string
}

export const mutations = {
  createSources: (body: SourceIn) => request<Source[]>("POST", "/api/sources", body),
  updateSource: (id: number, body: SourcePatch) =>
    request<Source>("PATCH", `/api/sources/${id}`, body),
  pollSource: (id: number) => request<Source>("POST", `/api/sources/${id}/poll`),
  deleteSource: (id: number) => request<void>("DELETE", `/api/sources/${id}`),
  updateChannel: (id: string, body: Partial<TitleWords>) =>
    request<Channel>("PATCH", `/api/channels/${encodeURIComponent(id)}`, body),
  replaceDownloadSettings: (body: DownloadSettingsIn) =>
    request<DownloadSettings>("PUT", "/api/download-settings", body),
  replaceGlobalWords: (body: TitleWords) => request<TitleWords>("PUT", "/api/words", body),
  createRule: (body: RuleIn) => request<Rule>("POST", "/api/rules", body),
  replaceRule: (id: number, body: RuleIn) => request<Rule>("PUT", `/api/rules/${id}`, body),
  deleteRule: (id: number) => request<void>("DELETE", `/api/rules/${id}`),
}

export const api = {
  me: () => get<Me>("/api/me"),
  statsSummary: () => get<StatsSummary>("/api/stats/summary"),
  statsDaily: (days: number) => get<DailyStat[]>("/api/stats/daily", { days }),
  channels: () => get<Channel[]>("/api/channels"),
  sources: () => get<Source[]>("/api/sources"),
  rules: () => get<Rule[]>("/api/rules"),
  globalWords: () => get<TitleWords>("/api/words"),
  downloadSettings: () => get<DownloadSettings>("/api/download-settings"),
  // A POST that writes nothing: it lists live through yt-dlp, which can answer 502 or 504.
  channelPlaylists: (url: string) =>
    request<ChannelPlaylist[]>("POST", "/api/channel-playlists", { url }),
  channelWords: (id: string) => get<TitleWord[]>(`/api/channels/${encodeURIComponent(id)}/words`),
  // A POST that writes nothing: the word lists ride in the body.
  titlePreview: (id: number, body: TitleWords) =>
    request<TitlePreview>("POST", `/api/sources/${id}/title-preview`, body),
  jobs: (query: { kind?: JobKind; state?: JobState; limit?: number }) =>
    get<Job[]>("/api/jobs", query),
  videos: (query: {
    channel_id?: string
    type?: string
    q?: string
    limit?: number
    offset?: number
  }) => get<VideoPage>("/api/videos", query),
  video: (id: string) => get<VideoDetail>(`/api/videos/${encodeURIComponent(id)}`),
}
