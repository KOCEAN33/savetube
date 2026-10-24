import { Fragment, useState, type MouseEvent } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ChannelYouTubeLink, VideoYouTubeLink } from "@/components/youtube-link"
import type { Job, JobState, MissReason } from "@/lib/api"
import { JOB_STATES, jobStateVariant } from "@/lib/channel"
import { formatClock, formatDateTime } from "@/lib/format"

const MISS_REASON_LABEL: Record<MissReason, string> = {
  dvr_off: "DVR off",
  ended_before_detection: "ended before detection",
  recorder_failed: "recorder failed",
}

// A succeeded or cancelled job's log_tail explains no failure.
function hasFailureReason(job: Job) {
  return (job.state === "failed" || job.state === "interrupted") && job.reason !== null
}

export function JobStateCounts({ jobs }: { jobs: Job[] }) {
  const counts = new Map<JobState, number>()
  for (const job of jobs) counts.set(job.state, (counts.get(job.state) ?? 0) + 1)
  return (
    <div className="flex flex-wrap gap-2 border-b p-3 text-sm">
      <span className="text-muted-foreground">Last {jobs.length} job(s):</span>
      {JOB_STATES.filter((s) => counts.has(s)).map((s) => (
        <Badge key={s} variant={jobStateVariant(s)}>
          {s} {counts.get(s)}
        </Badge>
      ))}
    </div>
  )
}

export function JobsTable({
  jobs,
  channelName,
  scheduled = false,
  expandLog = false,
  empty,
}: {
  jobs: Job[]
  // Set to show a channel column.
  channelName?: Map<string, string>
  // Show the announced start, which only a recording has.
  scheduled?: boolean
  // A click on a failed or interrupted row opens its full log_tail below it.
  expandLog?: boolean
  empty: string
}) {
  const [open, setOpen] = useState<Set<number>>(new Set())
  const columns = 6 + (channelName ? 1 : 0) + (scheduled ? 1 : 0)
  return (
    <Table>
      <TableHeader className="bg-muted">
        <TableRow>
          <TableHead>Video</TableHead>
          {channelName && <TableHead>Channel</TableHead>}
          <TableHead>State</TableHead>
          <TableHead className="text-right">Attempts</TableHead>
          {scheduled && <TableHead>Scheduled start</TableHead>}
          <TableHead>{scheduled ? "Detected" : "Queued"}</TableHead>
          <TableHead>Started</TableHead>
          <TableHead>Finished</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {jobs.length === 0 && (
          <TableRow>
            <TableCell colSpan={columns} className="text-center text-muted-foreground">
              {empty}
            </TableCell>
          </TableRow>
        )}
        {jobs.map((j) => {
          const expandable = expandLog && hasFailureReason(j) && j.log_tail !== null
          const expanded = expandable && open.has(j.id)
          function toggle(e: MouseEvent) {
            if ((e.target as HTMLElement).closest("a")) return
            const next = new Set(open)
            if (expanded) next.delete(j.id)
            else next.add(j.id)
            setOpen(next)
          }
          return (
            <Fragment key={j.id}>
              <TableRow
                className={expandable ? "cursor-pointer" : undefined}
                onClick={expandable ? toggle : undefined}
              >
                {/* w-full with max-w-0 gives this column the leftover width and truncates
                    inside it. min-w-48 keeps a title readable once the other columns fill
                    a narrow table, which then scrolls sideways. */}
                <TableCell className="w-full max-w-0 min-w-48">
                  {j.video_id ? (
                    <div className="flex items-center gap-2">
                      <Link
                        className="min-w-0 truncate hover:underline"
                        title={j.title ?? j.video_id}
                        to={`/videos/${encodeURIComponent(j.video_id)}`}
                      >
                        {j.title ?? j.video_id}
                      </Link>
                      <VideoYouTubeLink id={j.video_id} title={j.title} iconOnly />
                    </div>
                  ) : (
                    "-"
                  )}
                </TableCell>
                {channelName && (
                  <TableCell>
                    {j.channel_id ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Link
                          className="hover:underline"
                          to={`/channels/${encodeURIComponent(j.channel_id)}`}
                        >
                          {channelName.get(j.channel_id) ?? j.channel_id}
                        </Link>
                        <ChannelYouTubeLink
                          id={j.channel_id}
                          name={channelName.get(j.channel_id)}
                        />
                      </span>
                    ) : (
                      "-"
                    )}
                  </TableCell>
                )}
                {/* The reason wraps, so a long one does not take the title's width.
                    With expandLog it truncates instead, so a closed row stays one line. */}
                <TableCell className={expandLog ? "whitespace-nowrap" : "min-w-40 whitespace-normal"}>
                  <Badge variant={jobStateVariant(j.state)}>{j.state}</Badge>
                  {j.state === "queued" && j.deferred_until && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      waits until {formatClock(j.deferred_until)}
                    </span>
                  )}
                  {j.miss_reason && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      {MISS_REASON_LABEL[j.miss_reason]}
                    </span>
                  )}
                  {expandable ? (
                    // No handler of its own: the click reaches the row's toggle.
                    <button
                      type="button"
                      className="ml-2 inline-block max-w-56 truncate align-middle text-xs text-muted-foreground hover:underline"
                      aria-expanded={expanded}
                      title={j.log_tail ?? undefined}
                    >
                      {j.reason}
                    </button>
                  ) : (
                    hasFailureReason(j) && (
                      <span
                        className="ml-2 text-xs text-muted-foreground"
                        title={j.log_tail ?? undefined}
                      >
                        {j.reason}
                      </span>
                    )
                  )}
                </TableCell>
                <TableCell className="text-right">{j.attempts}</TableCell>
                {scheduled && <TableCell>{formatDateTime(j.scheduled_at)}</TableCell>}
                <TableCell>{formatDateTime(j.created_at)}</TableCell>
                <TableCell>{formatDateTime(j.started_at)}</TableCell>
                <TableCell>{formatDateTime(j.finished_at)}</TableCell>
              </TableRow>
              {expanded && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={columns} className="max-w-0">
                    <pre className="max-h-60 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap break-all">
                      {j.log_tail}
                    </pre>
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}
