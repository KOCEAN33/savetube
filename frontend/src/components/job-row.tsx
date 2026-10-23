import { Link } from "react-router"

import { type Job } from "@/lib/api"
import { STEP_LABELS } from "@/lib/channel"
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format"


function ProgressLine({ job }: { job: Job }) {
  const p = job.progress
  if (!p)
    return (
      <p className="text-xs text-muted-foreground">
        Preparing. Metadata, subtitles and the chat replay come before the media.
      </p>
    )
  if ("step" in p) return <p className="text-xs text-muted-foreground">{STEP_LABELS[p.step]}</p>
  const parts = [
    p.pct !== null ? `${p.pct.toFixed(1)}%` : null,
    p.done !== null && p.total !== null ? `${formatBytes(p.done)} of ${formatBytes(p.total)}` : null,
    p.done !== null && p.total === null ? formatBytes(p.done) : null,
    p.speed !== null ? `${formatBytes(p.speed)}/s` : null,
    p.eta !== null ? `ETA ${formatDuration(p.eta)}` : null,
    p.format ? `format ${p.format}` : null,
  ].filter(Boolean)
  return (
    <div className="flex flex-col gap-1">
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuenow={p.pct ?? undefined}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="h-full bg-primary transition-[width]" style={{ width: `${p.pct ?? 0}%` }} />
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">{parts.join(" · ")}</p>
    </div>
  )
}

export function ChannelLink({ job }: { job: Job }) {
  if (!job.channel_id) return <span className="text-xs text-muted-foreground">-</span>
  return (
    <Link
      className="w-fit text-xs text-muted-foreground hover:underline"
      to={`/channels/${encodeURIComponent(job.channel_id)}`}
    >
      {job.channel_name ?? job.channel_id}
    </Link>
  )
}

// `withChannel` names the video's uploader, for a list that spans channels.
export function RunningJob({ job, withChannel = false }: { job: Job; withChannel?: boolean }) {
  return (
    <li className="flex flex-col gap-2 p-3">
      {withChannel && <ChannelLink job={job} />}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        {job.video_id ? (
          <Link
            className="min-w-0 truncate font-medium hover:underline"
            title={job.title ?? job.video_id}
            to={`/videos/${encodeURIComponent(job.video_id)}`}
          >
            {job.title ?? job.video_id}
          </Link>
        ) : (
          <span className="font-medium">Job {job.id}</span>
        )}
        <span className="text-xs text-muted-foreground">
          started {formatDateTime(job.started_at)}
        </span>
      </div>
      <ProgressLine job={job} />
    </li>
  )
}
