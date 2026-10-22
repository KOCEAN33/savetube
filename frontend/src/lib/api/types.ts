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
  // Members-only videos skip the title words. A confirmed member profile is still needed.
  members_all: boolean
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
  // The video's title and its uploader channel. Null for a job without a video.
  title: string | null
  channel_id: string | null
  channel_name: string | null
  // The run's error or interruption text, and one plain line made from it.
  log_tail: string | null
  reason: string | null
  attempts: number
  miss_reason: MissReason | null
  scheduled_at: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  // A queued archive job waiting for the download window or a free slot.
  deferred_until: string | null
  // A running download's current stream, updated every few seconds, or the
  // step after its last byte.
  progress: JobProgress | JobStep | null
}

export interface Staging {
  count: number
  bytes: number
  // A Move now the scheduler has not taken yet.
  move_requested: boolean
}

export interface JobStep {
  step: "merging" | "verifying" | "audio"
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

// A stored `<video id>.<lang>.vtt`. The label is yt-dlp's track name, else the code.
export interface Subtitle {
  lang: string
  label: string
}

export type VideoOrder = "newest" | "oldest"

export interface VideoPage {
  total: number
  items: Video[]
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

export type ChannelPatch = Partial<TitleWords & ChannelSettings & Pick<Channel, "members_all">>

export interface DownloadSettingsIn {
  max_concurrent: number
  rate_limit_bytes: number | null
  window_start: string | null
  window_end: string | null
  // Off keeps the times and ignores them.
  window_enabled: boolean
  // Archive downloads per Asia/Seoul day. Null: no cap.
  daily_limit: number | null
  // The profile of every poll, live probe and non-members download. Null: cookie-free.
  cookie_profile: string | null
}

export interface DownloadSettings extends DownloadSettingsIn {
  timezone: string
  // The user's pause of archive downloads. Set only through setDownloadsPaused.
  paused: boolean
  // Archive downloads succeeded today, plus running ones. Read-only.
  downloaded_today: number
}
