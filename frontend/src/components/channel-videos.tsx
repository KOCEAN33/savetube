import { useState } from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { ChevronRightIcon } from "lucide-react"
import { Link } from "react-router"

import { Clock } from "@/components/clock"
import { Pagination } from "@/components/pagination"
import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { VideoYouTubeLink } from "@/components/youtube-link"
import { usePageParam } from "@/hooks/use-page-param"
import { api, type Source, type Video, type VideoOrder, type VideoType } from "@/lib/api"
import { sourceLabel, thumbUrl } from "@/lib/channel"
import { formatBytes, formatCount, formatDuration, formatUploadDate } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { cn } from "@/lib/utils"

const PAGE = 30
// A tab's list keeps its page under `page`, each playlist fold under its own key.
const PAGE_KEY = "page"
const playlistPageKey = (sourceId: number) => `pl${sourceId}`

// Which stored videos a list shows: the channel's by the channel list rule, or one source's.
export type VideoScope = { listedBy: string; type: VideoType } | { sourceId: number }

const videoHref = (id: string) => `/videos/${encodeURIComponent(id)}`

// The YouTube link sits on the thumbnail's top left corner.
const OVERLAY = "absolute top-3 left-4 rounded bg-background/80 p-1 text-foreground"

function useStoredVideos(scope: VideoScope, q: string, order: VideoOrder, page: number) {
  const filter =
    "sourceId" in scope
      ? { source_id: scope.sourceId }
      : { listed_by: scope.listedBy, type: scope.type }
  return useQuery({
    queryKey: queryKeys.videoPage({ ...filter, q, order, page }),
    queryFn: () =>
      api.videos({
        ...filter,
        state: "complete",
        q,
        order,
        limit: PAGE,
        offset: (page - 1) * PAGE,
      }),
    placeholderData: keepPreviousData,
  })
}

function VideoRow({ video }: { video: Video }) {
  const meta = [formatUploadDate(video.upload_date), formatDuration(video.duration), formatBytes(video.size_bytes)]
  return (
    <li className="relative">
      <Link
        to={videoHref(video.id)}
        className="grid grid-cols-[7rem_minmax(0,1fr)] items-center gap-3 px-3 py-2 text-sm hover:bg-muted/60 @2xl:grid-cols-[8rem_minmax(0,1fr)_6rem_4.5rem_5rem] @2xl:gap-4"
      >
        <img
          src={thumbUrl(video.id, "mq")}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          className="aspect-video w-full rounded-md bg-muted object-cover"
        />
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="line-clamp-2 font-medium" title={video.title}>
            {video.title}
          </span>
          <span className="text-xs text-muted-foreground tabular-nums @2xl:hidden">
            {meta.join(" · ")}
          </span>
        </div>
        {meta.map((m, i) => (
          <span
            key={i}
            className={cn(
              "hidden text-muted-foreground tabular-nums @2xl:block",
              i > 0 && "text-right",
            )}
          >
            {m}
          </span>
        ))}
      </Link>
      {/* Over the thumbnail's corner, outside the row link: a link cannot nest in one. */}
      <VideoYouTubeLink id={video.id} title={video.title} iconOnly className={OVERLAY} />
    </li>
  )
}

function VideoRows({ videos, total }: { videos: Video[]; total: number }) {
  return (
    <div className="@container">
      <div className="hidden grid-cols-[8rem_minmax(0,1fr)_6rem_4.5rem_5rem] gap-4 border-b px-3 py-2 text-xs font-medium text-muted-foreground @2xl:grid">
        <span />
        <span>Title ({formatCount(total)})</span>
        <span>Uploaded</span>
        <span className="text-right">Duration</span>
        <span className="text-right">Size</span>
      </div>
      <ul className="flex flex-col divide-y">
        {videos.map((v) => (
          <VideoRow key={v.id} video={v} />
        ))}
      </ul>
    </div>
  )
}

function ShortTile({ video }: { video: Video }) {
  return (
    <div className="relative">
      <Link to={videoHref(video.id)} className="group flex flex-col gap-1.5">
        {/* The 9:16 frame. A Short without one falls back to the 4:3 frame, cropped. */}
        <img
          src={thumbUrl(video.id, "oar")}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={(e) => {
            const fallback = thumbUrl(video.id, "hq")
            if (e.currentTarget.src !== fallback) e.currentTarget.src = fallback
          }}
          className="aspect-9/16 w-full rounded-lg bg-muted object-cover"
        />
        <span className="line-clamp-2 text-sm font-medium group-hover:underline" title={video.title}>
          {video.title}
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {formatUploadDate(video.upload_date)} · {formatBytes(video.size_bytes)}
        </span>
      </Link>
      <VideoYouTubeLink
        id={video.id}
        title={video.title}
        iconOnly
        className={cn(OVERLAY, "top-1 left-1")}
      />
    </div>
  )
}

// Complete videos in the scope, one numbered page at a time, filtered by `q`.
export function StoredVideos({
  scope,
  q,
  order,
  pageKey = PAGE_KEY,
  tiles = false,
}: {
  scope: VideoScope
  q: string
  order: VideoOrder
  // The URL parameter that holds the page, one per list on the screen.
  pageKey?: string
  // Shorts show as 9:16 tiles, everything else as rows.
  tiles?: boolean
}) {
  const [page, setPage] = usePageParam(pageKey)
  const query = useStoredVideos(scope, q, order, page)
  if (query.isPending) return <Skeleton className="h-48 w-full" />
  if (query.isError) return <ErrorBox error={query.error} />
  const { items: videos, total } = query.data
  if (total === 0)
    return (
      <p className="p-6 text-center text-sm text-muted-foreground">
        {q ? `No stored video matches "${q}".` : "No video is stored here yet."}
      </p>
    )
  return (
    <div className="flex flex-col">
      {tiles ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-3 p-3">
          {videos.map((v) => (
            <ShortTile key={v.id} video={v} />
          ))}
        </div>
      ) : (
        <VideoRows videos={videos} total={total} />
      )}
      <Pagination page={page} total={total} pageSize={PAGE} onPage={setPage} label="Video pages" />
    </div>
  )
}

function PlaylistFold({ source, order }: { source: Source; order: VideoOrder }) {
  const [open, setOpen] = useState(false)
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm hover:bg-muted/60"
      >
        <ChevronRightIcon
          className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")}
        />
        <span className="min-w-0 truncate font-medium" title={sourceLabel(source)}>
          {sourceLabel(source)}
        </span>
        {!source.enabled && <Badge variant="outline">disabled</Badge>}
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">
          last poll {source.last_polled_at ? <Clock iso={source.last_polled_at} /> : "waiting"}
        </span>
      </button>
      {open && (
        <div className="border-t bg-muted/20">
          <StoredVideos
            scope={{ sourceId: source.id }}
            q=""
            order={order}
            pageKey={playlistPageKey(source.id)}
          />
        </div>
      )}
    </li>
  )
}

// Each playlist source, picked or under "all playlists", folds out to its stored videos.
// `q` matches the playlist title.
export function PlaylistFolds({
  playlists,
  q,
  order,
}: {
  playlists: Source[]
  q: string
  order: VideoOrder
}) {
  const term = q.toLowerCase()
  const shown = term
    ? playlists.filter((s) => sourceLabel(s).toLowerCase().includes(term))
    : playlists
  if (shown.length === 0)
    return (
      <p className="p-6 text-center text-sm text-muted-foreground">
        {q ? `No playlist matches "${q}".` : "No playlist is archived. Add one under Downloads."}
      </p>
    )
  return (
    <ul className="flex flex-col divide-y">
      {shown.map((s) => (
        <PlaylistFold key={s.id} source={s} order={order} />
      ))}
    </ul>
  )
}
