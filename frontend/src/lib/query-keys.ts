import type { JobState, TitleWords, VideoOrder, VideoType } from "@/lib/api"

// Every query key in the app. A key that starts with another key's elements
// is invalidated with it, so a key's leading elements must not change.
export const queryKeys = {
  me: () => ["me"] as const,
  channels: () => ["channels"] as const,
  sources: () => ["sources"] as const,
  words: () => ["words"] as const,
  downloadSettings: () => ["download-settings"] as const,
  cookieProfiles: () => ["cookie-profiles"] as const,
  staging: () => ["staging"] as const,
  statsSummary: () => ["stats", "summary"] as const,
  statsDaily: (days: number) => ["stats", "daily", days] as const,
  channelWords: (channelId: string | null) => ["channel-words", channelId] as const,
  channelPlaylists: (base: string) => ["channel-playlists", base] as const,
  // The prefix of every stored source's title preview.
  titlePreviews: () => ["title-preview"] as const,
  titlePreview: (sourceId: number, body: TitleWords) =>
    [...queryKeys.titlePreviews(), sourceId, body] as const,
  titlesPreview: (channelId: string | undefined, titles: string[], body: TitleWords) =>
    ["titles-preview", channelId, titles, body] as const,
  video: (id: string) => ["video", id] as const,
  subtitles: (videoId: string) => ["video", videoId, "subtitles"] as const,
  formatWatch: (videoId: string) => ["video", videoId, "formats"] as const,
  videos: (filter: {
    q: string
    type: string | undefined
    channelId: string | undefined
    channelNameParam: string | undefined
    listedBy: string | undefined
    vcodec: string | undefined
    acodec: string | undefined
    height: string | undefined
    premium: string | undefined
    offset: number
  }) => ["videos", filter] as const,
  typeTotal: (filter: { listedBy: string; type: VideoType; state: "complete"; limit: 1 }) =>
    ["videos", filter] as const,
  videoPages: (
    filter: ({ source_id: number } | { listed_by: string; type: VideoType }) & {
      q: string
      order: VideoOrder
    }
  ) => ["videos", "pages", filter] as const,
  // `scope` is a channel id, or "all" for every channel.
  archiveJobCounts: (scope: string) => ["jobs", "archive", scope, "counts"] as const,
  channelActiveJobs: (channelId: string) => ["jobs", "archive", channelId, "active"] as const,
  channelRecentJobs: (channelId: string, limit: number) =>
    ["jobs", "archive", channelId, "recent", limit] as const,
  runningJobs: () => ["jobs", "archive", "all", "running"] as const,
  problemJobs: () => ["jobs", "archive", "all", "problems"] as const,
  allActiveJobs: () => ["jobs", "archive", "all", "active"] as const,
  allRecentJobs: (filter: { state: JobState | undefined; unresolved: boolean; limit: number }) =>
    ["jobs", "archive", "all", "recent", filter] as const,
  recordJobs: (state: JobState | undefined) => ["jobs", "record", state] as const,
  recordingNow: () => ["jobs", "record", "now"] as const,
  scheduledRecords: () => ["jobs", "record", "scheduled"] as const,
  pods: () => ["pods"] as const,
}
