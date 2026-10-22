import type { JobState, Source, VideoTab } from "@/lib/api"

export const DEFAULT_FORMAT = "bv*+ba/b"

// YouTube's image host. No endpoint serves the stored thumbnail.
export const thumbUrl = (id: string, size: "mq" | "hq" | "oar") =>
  `https://i.ytimg.com/vi/${encodeURIComponent(id)}/${size}default.jpg`

export const VIDEO_TABS: { tab: VideoTab; label: string }[] = [
  { tab: "videos", label: "Videos" },
  { tab: "shorts", label: "Shorts" },
  { tab: "streams", label: "Live (past streams)" },
]

export const OTHER_CHANNELS_LABEL =
  "Also download videos other channels uploaded to these playlists"

export const OTHER_CHANNELS_HINT =
  "A video downloads once. It is stored under its uploader's channel folder and shows on this channel's page."

export const MEMBERS_ALL_LABEL = "Download every members-only video"

export const MEMBERS_ALL_HINT =
  "Title words do not apply to members-only videos. They download only while a cookie profile is a confirmed member of this channel."

export const PLAYLISTS_PASS_HINT =
  "Title words then only exclude in playlists. A video passes when its title passes the words or a chosen playlist lists it."

export const JOB_STATES: JobState[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "missed",
  "cancelled",
  "interrupted",
]

export function jobStateVariant(state: JobState) {
  if (state === "failed" || state === "missed") return "destructive" as const
  if (state === "succeeded") return "secondary" as const
  return "outline" as const
}

// The channel URL without a tab, from one of its tab or playlists sources.
export function channelBase(channelId: string, sources: Source[]): string {
  const own = sources.find((s) => s.channel_id === channelId && s.kind !== "playlist")
  if (own) return own.url.slice(0, own.url.lastIndexOf("/"))
  return `https://www.youtube.com/channel/${channelId}`
}

// Short name for a source: the playlist title, or the tab.
export function sourceLabel(s: Source): string {
  if (s.title) return s.title
  if (s.kind === "playlists") return "All playlists"
  const list = new URL(s.url).searchParams.get("list")
  if (list) return list
  const tab = s.url.slice(s.url.lastIndexOf("/") + 1)
  return VIDEO_TABS.find((t) => t.tab === tab)?.label ?? tab
}
