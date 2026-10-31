import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router"

import { Pagination } from "@/components/pagination"
import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ChannelYouTubeLink, VideoYouTubeLink } from "@/components/youtube-link"
import { usePageParam } from "@/hooks/use-page-param"
import { api, type Video, type VideoType } from "@/lib/api"
import {
  formatBytes,
  formatCount,
  formatDuration,
  formatKbps,
  formatUploadDate,
} from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"

const PAGE_SIZE = 50
const ALL = "all"
const TYPES: VideoType[] = ["videos", "shorts", "lives", "members"]
// The `vcodec`, `acodec` and `height` values GET /api/videos takes.
const VIDEO_CODECS = [
  ["av1", "AV1"],
  ["vp9", "VP9"],
  ["h264", "H.264"],
  ["other", "Other codecs"],
] as const
const AUDIO_CODECS = [
  ["opus", "Opus"],
  ["aac", "AAC"],
] as const
const HEIGHTS = [
  ["2160", "2160p+"],
  ["1440", "1440p"],
  ["1080", "1080p"],
  ["720", "720p"],
  ["below", "Below 720p"],
] as const

/** The short side and frame rate, "1080p60", so a vertical short reads 1080p. */
function resolution(v: Video): string {
  const side = Math.min(v.video_width ?? Infinity, v.video_height ?? Infinity)
  if (side === Infinity) return "-"
  return `${side}p${v.video_fps ? Math.round(v.video_fps) : ""}`
}

export function VideosPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get("q") ?? ""
  const type = params.get("type") ?? undefined
  const channelId = params.get("channel_id") ?? undefined
  // One entry per name, because a group's member channels share it.
  const channelNameParam = params.get("channel_name") ?? undefined
  // From a channel page: its uploads and the videos its sources list.
  const listedBy = params.get("listed_by") ?? undefined
  const vcodec = params.get("vcodec") ?? undefined
  const acodec = params.get("acodec") ?? undefined
  const height = params.get("height") ?? undefined
  // "true" or "false".
  const premium = params.get("premium") ?? undefined
  const [pageNo, setPage] = usePageParam()

  const channels = useQuery({ queryKey: queryKeys.channels(), queryFn: api.channels })
  const videos = useQuery({
    queryKey: queryKeys.videos({
      q,
      type,
      channelId,
      channelNameParam,
      listedBy,
      vcodec,
      acodec,
      height,
      premium,
      page: pageNo,
    }),
    queryFn: () =>
      api.videos({
        q,
        type,
        channel_id: channelId,
        channel_name: channelNameParam,
        listed_by: listedBy,
        vcodec,
        acodec,
        height,
        premium: premium === undefined ? undefined : premium === "true",
        limit: PAGE_SIZE,
        offset: (pageNo - 1) * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  })
  const channelName = new Map(channels.data?.map((c) => [c.id, c.name]))
  const names = [...new Set(channels.data?.map((c) => c.name))]
  const byId = channelId ?? listedBy

  function update(changes: Record<string, string | undefined>) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value === undefined || value === "" || value === ALL) next.delete(key)
      else next.set(key, value)
    }
    next.delete("page")
    setParams(next)
  }

  const filters = (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        const value = new FormData(e.currentTarget).get("q")
        update({ q: typeof value === "string" ? value.trim() : undefined })
      }}
    >
      <Input
        key={q}
        name="q"
        defaultValue={q}
        placeholder="Search titles"
        className="h-8 w-56"
      />
      <Select value={type ?? ALL} onValueChange={(v) => update({ type: v })}>
        <SelectTrigger size="sm" className="w-32" aria-label="Type">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All types</SelectItem>
          {TYPES.map((t) => (
            <SelectItem key={t} value={t}>
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={channelNameParam ?? (byId && channelName.get(byId)) ?? ALL}
        onValueChange={(v) =>
          update({ channel_name: v, channel_id: undefined, listed_by: undefined })
        }
      >
        <SelectTrigger size="sm" className="w-48" aria-label="Channel">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All channels</SelectItem>
          {names.map((name) => (
            <SelectItem key={name} value={name}>
              {name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select value={vcodec ?? ALL} onValueChange={(v) => update({ vcodec: v })}>
        <SelectTrigger size="sm" className="w-32" aria-label="Video codec">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All codecs</SelectItem>
          {VIDEO_CODECS.map(([value, label]) => (
            <SelectItem key={value} value={value}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select value={height ?? ALL} onValueChange={(v) => update({ height: v })}>
        <SelectTrigger size="sm" className="w-36" aria-label="Resolution">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All resolutions</SelectItem>
          {HEIGHTS.map(([value, label]) => (
            <SelectItem key={value} value={value}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select value={premium ?? ALL} onValueChange={(v) => update({ premium: v })}>
        <SelectTrigger size="sm" className="w-32" aria-label="Premium">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>Any format</SelectItem>
          <SelectItem value="true">Premium</SelectItem>
          <SelectItem value="false">Not Premium</SelectItem>
        </SelectContent>
      </Select>
      <Select value={acodec ?? ALL} onValueChange={(v) => update({ acodec: v })}>
        <SelectTrigger size="sm" className="w-28" aria-label="Audio codec">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All audio</SelectItem>
          {AUDIO_CODECS.map(([value, label]) => (
            <SelectItem key={value} value={value}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </form>
  )

  return (
    <Page>
      <PageSection title="Videos" actions={filters}>
        <QueryState query={videos}>
          {(page) => (
            <>
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>Title ({formatCount(page.total)})</TableHead>
                    <TableHead>Channel</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Uploaded</TableHead>
                    <TableHead className="text-right">Duration</TableHead>
                    <TableHead>Codec</TableHead>
                    <TableHead>Resolution</TableHead>
                    <TableHead className="text-right">Video bitrate</TableHead>
                    <TableHead>Format</TableHead>
                    <TableHead className="text-right">Size</TableHead>
                    <TableHead>State</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {page.items.map((v) => (
                    <TableRow key={v.id}>
                      <TableCell className="max-w-96 font-medium">
                        <div className="flex items-center gap-2">
                          <Link
                            className="min-w-0 truncate hover:underline"
                            to={`/videos/${encodeURIComponent(v.id)}`}
                          >
                            {v.title}
                          </Link>
                          <VideoYouTubeLink id={v.id} title={v.title} iconOnly />
                        </div>
                      </TableCell>
                      <TableCell>
                        <span className="inline-flex items-center gap-1.5">
                          {channelName.get(v.channel_id) ?? v.channel_id}
                          <ChannelYouTubeLink id={v.channel_id} name={channelName.get(v.channel_id)} />
                        </span>
                      </TableCell>
                      <TableCell>{v.type}</TableCell>
                      <TableCell>{formatUploadDate(v.upload_date)}</TableCell>
                      <TableCell className="text-right">{formatDuration(v.duration)}</TableCell>
                      <TableCell>
                        <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                          {v.video_codec ?? "-"}
                          {v.audio_codec && (
                            <span className="text-muted-foreground">{v.audio_codec}</span>
                          )}
                          {v.premium && <Badge variant="secondary">Premium</Badge>}
                        </span>
                      </TableCell>
                      <TableCell>{resolution(v)}</TableCell>
                      <TableCell className="text-right whitespace-nowrap">
                        {formatKbps(v.video_kbps)}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{v.format_id ?? "-"}</TableCell>
                      <TableCell className="text-right whitespace-nowrap">
                        {formatBytes(v.size_bytes)}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{v.state}</Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                  {page.items.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={11} className="h-24 text-center">
                        No videos.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
              <Pagination
                page={pageNo}
                total={page.total}
                pageSize={PAGE_SIZE}
                onPage={setPage}
                label="Video pages"
              />
            </>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
