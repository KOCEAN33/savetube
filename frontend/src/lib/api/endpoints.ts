import { get, getPage, request } from "@/lib/api/client"
import type {
  Channel,
  ChannelIn,
  ChannelPatch,
  ChannelPlaylist,
  ChannelScan,
  CookieProfile,
  DailyStat,
  DownloadSettings,
  DownloadSettingsIn,
  Encode,
  EncodeSettingsIn,
  FormatWatch,
  Job,
  JobKind,
  JobState,
  Me,
  Pods,
  Source,
  SourceIn,
  SourcePatch,
  Staging,
  StatsSummary,
  Subtitle,
  TitleKept,
  TitlePreview,
  TitleWord,
  TitleWords,
  VideoDetail,
  VideoOrder,
  VideoPage,
} from "@/lib/api/types"

type JobQuery = {
  kind?: JobKind
  state?: JobState[]
  channel_id?: string
  // Drop a failed or interrupted archive run once a later run of its video succeeded.
  unresolved?: boolean
  limit?: number
  // newest by default. dispatch: running first, then the order the worker takes the queue.
  order?: "newest" | "dispatch"
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
  // The scheduler moves every staged video on its next check, within a minute.
  moveStaged: () => request<Staging>("POST", "/api/staging/move"),
  setDownloadsPaused: (paused: boolean) =>
    request<DownloadSettings>("PUT", "/api/download-settings/paused", { paused }),
  replaceGlobalWords: (body: TitleWords) => request<TitleWords>("PUT", "/api/words", body),
  replaceEncodeSettings: (body: EncodeSettingsIn) => request<Encode>("PUT", "/api/encode", body),
  // Queued jobs only: 409 once the encoder claimed the job.
  cancelEncodeJob: (id: number) => request<Encode>("POST", `/api/encode/jobs/${id}/cancel`),
  // Failed jobs only: the video may be queued again.
  requeueEncodeJob: (id: number) => request<Encode>("POST", `/api/encode/jobs/${id}/requeue`),
  // Queues a replace with one video and one audio format at most, ends the
  // automatic replaces, and cancels a queued automatic one. A held video needs
  // `confirm`.
  pickFormat: (videoId: string, body: { format_ids: string[]; confirm: boolean }) =>
    request<FormatWatch>(
      "POST",
      `/api/videos/${encodeURIComponent(videoId)}/formats/pick`,
      body,
    ),
  // The scheduler probes the formats now. Up to 120 s while the format watch
  // probes another video first.
  probeFormats: (videoId: string) =>
    request<FormatWatch>("POST", `/api/videos/${encodeURIComponent(videoId)}/formats/probe`),
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
  encode: () => get<Encode>("/api/encode"),
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
  jobs: (query: JobQuery) => get<Job[]>("/api/jobs", query),
  // The same list with the count of every matching job, for numbered pages.
  jobPage: (query: JobQuery & { offset?: number }) => getPage<Job>("/api/jobs", query),
  // Every state is present, zero when no job holds it.
  jobCounts: (query: { kind?: JobKind; channel_id?: string }) =>
    get<Record<JobState, number>>("/api/jobs/counts", query),
  pods: () => get<Pods>("/api/pods"),
  // The videos waiting on staging for the move into the tree.
  staging: () => get<Staging>("/api/staging"),
  videos: (query: {
    // The uploader.
    channel_id?: string
    // Uploaders by name: a group's member channels share it under their own ids.
    channel_name?: string
    // The uploader, or a channel whose sources list the video.
    listed_by?: string
    // Videos the source lists. A `playlists` parent lists through its children.
    source_id?: number
    type?: string
    state?: string
    q?: string
    // The stored streams. `other` is a known video codec outside the three.
    vcodec?: string
    acodec?: string
    // A bucket of the video's short side: 2160, 1440, 1080, 720, below.
    height?: string
    premium?: boolean
    // By upload date. Default newest.
    order?: VideoOrder
    limit?: number
    offset?: number
  }) => get<VideoPage>("/api/videos", query),
  video: (id: string) => get<VideoDetail>(`/api/videos/${encodeURIComponent(id)}`),
  // A complete video's stored mkv and its Opus audio copy, served with Range.
  mediaUrl: (id: string) => `/api/videos/${encodeURIComponent(id)}/media`,
  audioUrl: (id: string) => `/api/videos/${encodeURIComponent(id)}/audio`,
  // Null for a video without a row: no format watch and no probe yet.
  formatWatch: (id: string) =>
    get<FormatWatch | null>(`/api/videos/${encodeURIComponent(id)}/formats`),
  subtitles: (id: string) => get<Subtitle[]>(`/api/videos/${encodeURIComponent(id)}/subtitles`),
  subtitleUrl: (id: string, lang: string) =>
    `/api/videos/${encodeURIComponent(id)}/subtitles/${encodeURIComponent(lang)}`,
}
