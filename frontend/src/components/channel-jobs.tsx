import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { JobsTable } from "@/components/jobs-table"
import { QueryState } from "@/components/query-state"
import { api, type Job } from "@/lib/api"
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format"

// The worker writes progress every 5 s, so a faster refetch shows nothing new.
const RUNNING_REFETCH_MS = 3_000
const IDLE_REFETCH_MS = 15_000
const QUEUED_SHOWN = 50
const RECENT = 20

function ProgressLine({ job }: { job: Job }) {
  const p = job.progress
  if (!p)
    return (
      <p className="text-xs text-muted-foreground">
        Preparing. Metadata, subtitles and the chat replay come before the media.
      </p>
    )
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

function RunningJob({ job }: { job: Job }) {
  return (
    <li className="flex flex-col gap-2 p-3">
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

// The running download first, then the queued jobs in the order the
// dispatcher takes them (oldest first), then the latest finished jobs.
export function ChannelJobs({ channelId }: { channelId: string }) {
  const active = useQuery({
    queryKey: ["jobs", "archive", channelId, "active"],
    queryFn: () =>
      api.jobs({ kind: "archive", channel_id: channelId, state: ["running", "queued"], limit: 500 }),
    refetchInterval: (q) =>
      q.state.data?.some((j) => j.state === "running") ? RUNNING_REFETCH_MS : IDLE_REFETCH_MS,
  })
  const busy = active.data?.some((j) => j.state === "running") ?? false
  const recent = useQuery({
    queryKey: ["jobs", "archive", channelId, "recent"],
    queryFn: () =>
      api.jobs({
        kind: "archive",
        channel_id: channelId,
        state: ["succeeded", "failed", "cancelled"],
        limit: RECENT,
      }),
    refetchInterval: busy ? RUNNING_REFETCH_MS : IDLE_REFETCH_MS,
  })

  return (
    <div className="flex flex-col">
      <QueryState query={active}>
        {(jobs) => {
          const running = jobs.filter((j) => j.state === "running")
          const queued = jobs.filter((j) => j.state === "queued").sort((a, b) => a.id - b.id)
          return (
            <>
              <h3 className="border-b bg-muted px-3 py-2 text-sm font-medium">Downloading now</h3>
              {running.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
              ) : (
                <ul className="flex flex-col divide-y">
                  {running.map((j) => (
                    <RunningJob key={j.id} job={j} />
                  ))}
                </ul>
              )}
              <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">
                Queued ({queued.length}), next first
              </h3>
              {queued.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">Nothing waits.</p>
              ) : (
                <JobsTable jobs={queued.slice(0, QUEUED_SHOWN)} empty="" />
              )}
              {queued.length > QUEUED_SHOWN && (
                <p className="p-3 text-xs text-muted-foreground">
                  {queued.length - QUEUED_SHOWN} more wait after these.
                </p>
              )}
            </>
          )
        }}
      </QueryState>
      <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">Recently finished</h3>
      <QueryState query={recent}>
        {(jobs) => <JobsTable jobs={jobs} empty="No finished job yet." />}
      </QueryState>
    </div>
  )
}
