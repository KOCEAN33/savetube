export type VideoType = "videos" | "shorts" | "lives" | "members"
export type JobKind = "archive" | "record"
export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "missed"
  | "cancelled"
  | "interrupted"
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
  // Null: no cutoff. Compared with YouTube's approximate upload date.
  date_after: string | null
  // A yt-dlp format selector.
  format: string
  watch_lives: boolean
  // The profile picture YouTube serves. Null until a scan or a tab poll finds it.
  avatar_url: string | null
  // False for a channel known only as the uploader of a video a source lists.
  tracked: boolean
  // Each cookie profile's last membership check, by profile name.
  memberships: Membership[]
}

export interface Membership {
  profile: string
  is_member: boolean
  checked_at: string
}

// A cookie file in SAVETUBE_COOKIES_DIR, by name. The API never sends its content.
export interface CookieProfile {
  name: string
}

export interface Source {
  id: number
  url: string
  kind: "tab" | "playlist" | "playlists"
  channel_id: string | null
  parent_id: number | null
  // The playlist's title for a `playlist` source. Null for a tab or `playlists` source.
  title: string | null
  // Off: polls list the source but download nothing.
  archiving: boolean
  include_words: string[]
  exclude_words: string[]
  other_channels: boolean
  enabled: boolean
  poll_interval_minutes: number
  last_polled_at: string | null
  last_error: string | null
}

export interface Job {
  id: number
  kind: JobKind
  state: JobState
  video_id: string | null
  // The video's title and channel. Null for a job without a video.
  title: string | null
  channel_id: string | null
  attempts: number
  miss_reason: MissReason | null
  scheduled_at: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  // A queued archive job waiting for the download window or a free slot.
  deferred_until: string | null
  // A running download's current stream, updated every few seconds.
  progress: JobProgress | null
}

export interface JobProgress {
  format: string | null
  // Null when YouTube gives no size.
  pct: number | null
  done: number | null
  total: number | null
  // Bytes per second.
  speed: number | null
  // Seconds.
  eta: number | null
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
  // The integrity checks the stored download passed: size, probe, packets.
  verified_at: string | null
  verified_checks: string[] | null
}

export interface VideoDetail extends Video {
  // yt-dlp's info, null until a download completes.
  info: Record<string, unknown> | null
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
    if (Array.isArray(value)) for (const v of value) params.append(key, v)
    else if (value !== undefined && value !== "") params.set(key, String(value))
  }
  const qs = params.toString()
  return request<T>("GET", qs ? `${path}?${qs}` : path)
}

export type Tab = "videos" | "shorts" | "streams" | "playlists"

export type VideoTab = Exclude<Tab, "playlists">

export interface SourceIn {
  url: string
  tabs?: Tab[]
  poll_interval_minutes?: number
  include_words?: string[]
  exclude_words?: string[]
  other_channels?: boolean
  archiving?: boolean
}

export type SourcePatch = Partial<
  Pick<
    Source,
    | "enabled"
    | "poll_interval_minutes"
    | "include_words"
    | "exclude_words"
    | "other_channels"
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

export interface ScanEntry {
  video_id: string
  title: string
}

export interface ChannelScan {
  // The channel URL without a tab.
  url: string
  channel_id: string
  handle: string | null
  name: string
  avatar_url: string | null
  // The first page of each tab. Null for a tab the channel lacks.
  tabs: Record<VideoTab, ScanEntry[] | null>
  playlists: { id: string; title: string; url: string }[]
  suggestions: TitleWord[]
}

export interface TitleKept {
  title: string
  kept: boolean
}

export interface ChannelSettings {
  date_after: string | null
  format: string
  watch_lives: boolean
}

export interface ChannelIn extends TitleWords, ChannelSettings {
  url: string
  channel_id: string
  handle: string | null
  name: string
  avatar_url: string | null
  tabs: VideoTab[]
  all_playlists: boolean
  playlists: { url: string; title: string }[]
  other_channels: boolean
}

export type ChannelPatch = Partial<TitleWords & ChannelSettings>

export interface DownloadSettingsIn {
  max_concurrent: number
  rate_limit_bytes: number | null
  window_start: string | null
  window_end: string | null
  // The profile of every poll, live probe and non-members download. Null: cookie-free.
  cookie_profile: string | null
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
  createChannel: (body: ChannelIn) =>
    request<Channel & { sources: Source[] }>("POST", "/api/channels", body),
  updateChannel: (id: string, body: ChannelPatch) =>
    request<Channel>("PATCH", `/api/channels/${encodeURIComponent(id)}`, body),
  replaceDownloadSettings: (body: DownloadSettingsIn) =>
    request<DownloadSettings>("PUT", "/api/download-settings", body),
  replaceGlobalWords: (body: TitleWords) => request<TitleWords>("PUT", "/api/words", body),
}

export const api = {
  me: () => get<Me>("/api/me"),
  statsSummary: () => get<StatsSummary>("/api/stats/summary"),
  statsDaily: (days: number) => get<DailyStat[]>("/api/stats/daily", { days }),
  channels: () => get<Channel[]>("/api/channels"),
  sources: () => get<Source[]>("/api/sources"),
  globalWords: () => get<TitleWords>("/api/words"),
  downloadSettings: () => get<DownloadSettings>("/api/download-settings"),
  cookieProfiles: () => get<CookieProfile[]>("/api/cookie-profiles"),
  // A POST that writes nothing: it lists live through yt-dlp, which can answer 502 or 504.
  channelPlaylists: (url: string) =>
    request<ChannelPlaylist[]>("POST", "/api/channel-playlists", { url }),
  channelWords: (id: string) => get<TitleWord[]>(`/api/channels/${encodeURIComponent(id)}/words`),
  // A POST that writes nothing: the word lists ride in the body.
  titlePreview: (id: number, body: TitleWords) =>
    request<TitlePreview>("POST", `/api/sources/${id}/title-preview`, body),
  // A POST that writes nothing: it lists the channel live through yt-dlp, which can answer 502 or 504.
  channelScan: (url: string) => request<ChannelScan>("POST", "/api/channel-scan", { url }),
  // A POST that writes nothing: titles that are not stored, such as a scan's.
  titlesPreview: (body: TitleWords & { titles: string[]; channel_id?: string }) =>
    request<TitleKept[]>("POST", "/api/title-preview", body),
  jobs: (query: { kind?: JobKind; state?: JobState[]; channel_id?: string; limit?: number }) =>
    get<Job[]>("/api/jobs", query),
  videos: (query: {
    // The uploader.
    channel_id?: string
    // The uploader, or a channel whose sources list the video.
    listed_by?: string
    type?: string
    state?: string
    q?: string
    limit?: number
    offset?: number
  }) => get<VideoPage>("/api/videos", query),
  video: (id: string) => get<VideoDetail>(`/api/videos/${encodeURIComponent(id)}`),
}
