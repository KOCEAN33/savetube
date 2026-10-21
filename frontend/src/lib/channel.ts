import type { JobState, Source, VideoTab } from "@/lib/api"

export const DEFAULT_FORMAT = "bv*+ba/b"

export const VIDEO_TABS: { tab: VideoTab; label: string }[] = [
  { tab: "videos", label: "Videos" },
  { tab: "shorts", label: "Shorts" },
  { tab: "streams", label: "Live (past streams)" },
]

export const OTHER_CHANNELS_LABEL =
  "Also download videos other channels uploaded to these playlists"

export const OTHER_CHANNELS_HINT =
  "A video downloads once. It is stored under its uploader's channel folder and shows on this channel's page."

export const JOB_STATES: JobState[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "missed",
  "cancelled",
  "interrupted",
]

// The channel URL without a tab, from one of its tab or playlists sources.
export function channelBase(channelId: string, sources: Source[]): string {
  const own = sources.find((s) => s.channel_id === channelId && s.kind !== "playlist")
  if (own) return own.url.slice(0, own.url.lastIndexOf("/"))
  return `https://www.youtube.com/channel/${channelId}`
}
