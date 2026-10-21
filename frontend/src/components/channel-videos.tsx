import { useState } from "react"
import { keepPreviousData, useQueries } from "@tanstack/react-query"
import { Link } from "react-router"

import { ErrorBox } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { api, type Video, type VideoType } from "@/lib/api"
import { formatBytes, formatCount, formatDuration, formatUploadDate } from "@/lib/format"

const GROUPS: { type: VideoType; label: string }[] = [
  { type: "videos", label: "Videos" },
  { type: "shorts", label: "Shorts" },
  { type: "lives", label: "Lives" },
  { type: "members", label: "Members only" },
]
const PAGE = 12

function VideoCard({ video }: { video: Video }) {
  return (
    <Link
      to={`/videos/${encodeURIComponent(video.id)}`}
      className="group flex flex-col gap-1.5 rounded-md p-1 hover:bg-muted"
    >
      {/* YouTube's image host. The stored thumbnail sits on the archive volume, which web does not mount. */}
      <img
        src={`https://i.ytimg.com/vi/${encodeURIComponent(video.id)}/mqdefault.jpg`}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        className="aspect-video w-full rounded bg-muted object-cover"
      />
      <span className="line-clamp-2 text-sm font-medium group-hover:underline" title={video.title}>
        {video.title}
      </span>
      <span className="text-xs text-muted-foreground tabular-nums">
        {formatUploadDate(video.upload_date)} · {formatDuration(video.duration)} ·{" "}
        {formatBytes(video.size_bytes)}
      </span>
    </Link>
  )
}

// Stored videos the channel uploaded or its sources list, one group per type,
// newest upload first. A collaboration is stored under the uploader its info
// names, so this uses `listed_by`, not the uploader filter.
export function ChannelVideos({ channelId }: { channelId: string }) {
  const [limits, setLimits] = useState<Record<string, number>>({})
  const results = useQueries({
    queries: GROUPS.map(({ type }) => {
      const limit = limits[type] ?? PAGE
      return {
        queryKey: ["videos", { listedBy: channelId, type, state: "complete", limit }],
        queryFn: () => api.videos({ listed_by: channelId, type, state: "complete", limit }),
        placeholderData: keepPreviousData,
      }
    }),
  })

  if (results.some((r) => r.isPending)) return <Skeleton className="h-40 w-full" />
  const failed = results.find((r) => r.isError)
  if (failed?.error) return <ErrorBox error={failed.error} />
  const groups = GROUPS.map((g, i) => ({ ...g, page: results[i].data })).filter(
    (g) => g.page && g.page.total > 0,
  )
  if (groups.length === 0)
    return <p className="p-4 text-sm text-muted-foreground">No video is stored yet.</p>

  return (
    <div className="flex flex-col divide-y">
      {groups.map(({ type, label, page }) =>
        page ? (
          <div key={type} className="flex flex-col gap-3 p-4">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">
                {label} ({formatCount(page.total)})
              </h3>
              <Link
                className="text-xs text-muted-foreground hover:underline"
                to={`/videos?listed_by=${encodeURIComponent(channelId)}&type=${type}`}
              >
                Open in Videos
              </Link>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
              {page.items.map((v) => (
                <VideoCard key={v.id} video={v} />
              ))}
            </div>
            {page.items.length < page.total && (
              <Button
                variant="outline"
                size="sm"
                className="w-fit"
                onClick={() =>
                  setLimits({ ...limits, [type]: Math.min((limits[type] ?? PAGE) + PAGE * 2, 200) })
                }
                disabled={page.items.length >= 200}
              >
                Show more
              </Button>
            )}
          </div>
        ) : null,
      )}
    </div>
  )
}
