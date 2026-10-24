import { Fragment, useState, type MouseEvent } from "react"
import { Link } from "react-router"

import { Clock } from "@/components/clock"
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
import { recorderOf } from "@/hooks/use-pods"
import type { Job, JobState, MissReason, Pods } from "@/lib/api"
import { JOB_STATES, jobStateVariant } from "@/lib/channel"
import { formatClock, formatDateTime, formatLength } from "@/lib/format"
import { cn } from "@/lib/utils"

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

// A record job not started yet shows its announced start, dimmed.
function LiveStart({ job }: { job: Job }) {
  const title = [
    job.scheduled_at && `Scheduled ${formatDateTime(job.scheduled_at)}`,
    `Detected ${formatDateTime(job.created_at)}`,
    job.started_at && `Started ${formatDateTime(job.started_at)}`,
  ]
    .filter(Boolean)
    .join("\n")
  return (
    <span title={title} className={job.started_at ? undefined : "text-muted-foreground"}>
      {formatClock(job.started_at ?? job.scheduled_at)}
    </span>
  )
}

function liveLength(job: Job): string {
  if (!job.started_at) return "-"
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now()
  return formatLength((end - Date.parse(job.started_at)) / 1000)
}

// The recorder Job's number is the record job's id. A missing Job is one not made
// yet or one the TTL removed, so only a retry or a failure is worth a word.
function RecorderCell({ pods, jobId }: { pods: Pods | undefined; jobId: number }) {
  const recorder = recorderOf(pods, jobId)
  if (!recorder) return <span className="text-muted-foreground">-</span>
  const alerts = [
    recorder.status === "failed" && "failed",
    recorder.restarts > 0 &&
      `${recorder.restarts} ${recorder.restarts === 1 ? "retry" : "retries"}`,
  ].filter(Boolean)
  return (
    <span title={recorder.name}>
      {jobId}
      {alerts.length > 0 && <span className="ml-2 text-destructive">{alerts.join(", ")}</span>}
    </span>
  )
}

export function JobsTable({
  jobs,
  channelName,
  live = false,
  pods,
  expandLog = false,
  empty,
}: {
  jobs: Job[]
  // Set to show a channel column.
  channelName?: Map<string, string>
  // Record jobs: one start, the length and the recorder Job, in place of the
  // queued, started and finished times.
  live?: boolean
  // The recorder Jobs for the live table's Recorder column.
  pods?: Pods
  // A click on a failed or interrupted row opens its full log_tail below it.
  expandLog?: boolean
  empty: string
}) {
  const [open, setOpen] = useState<Set<number>>(new Set())
  const columns = (live ? 6 : 5) + (channelName ? 1 : 0)
  // Fixed widths outside the title, so the queued and finished tables of one
  // page line up whatever their rows hold. A narrow screen scrolls sideways.
  const fixed = !live
  return (
    <Table className={fixed ? "min-w-5xl table-fixed" : undefined}>
      <TableHeader className="bg-muted">
        <TableRow>
          <TableHead>Video</TableHead>
          {channelName && <TableHead className={fixed ? "w-40" : undefined}>Channel</TableHead>}
          <TableHead className={fixed ? "w-60" : undefined}>State</TableHead>
          {live ? (
            <>
              <TableHead>Start</TableHead>
              <TableHead className="text-right">Length</TableHead>
              <TableHead>Recorder</TableHead>
            </>
          ) : (
            <>
              <TableHead className="w-28">Queued</TableHead>
              <TableHead className="w-28">Started</TableHead>
              <TableHead className="w-28">Finished</TableHead>
            </>
          )}
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
                      <span
                        className={cn(
                          "inline-flex max-w-full items-center gap-1.5",
                          !fixed && "max-w-48",
                        )}
                      >
                        <Link
                          className="min-w-0 truncate hover:underline"
                          title={channelName.get(j.channel_id) ?? j.channel_id}
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
                  {j.attempts > 1 && (
                    <span
                      className="ml-2 text-xs text-muted-foreground"
                      title={`${j.attempts} attempts`}
                    >
                      ×{j.attempts}
                    </span>
                  )}
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
                      className={cn(
                        "ml-2 inline-block truncate align-middle text-xs text-muted-foreground hover:underline",
                        // The fixed State column holds the widest badge, an attempt count and this.
                        fixed ? "max-w-32" : "max-w-56",
                      )}
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
                {live ? (
                  <>
                    <TableCell className="tabular-nums">
                      <LiveStart job={j} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{liveLength(j)}</TableCell>
                    <TableCell className="tabular-nums">
                      <RecorderCell pods={pods} jobId={j.id} />
                    </TableCell>
                  </>
                ) : (
                  <>
                    <TableCell className="truncate tabular-nums">
                      <Clock iso={j.created_at} />
                    </TableCell>
                    <TableCell className="truncate tabular-nums">
                      <Clock iso={j.started_at} />
                    </TableCell>
                    <TableCell className="truncate tabular-nums">
                      <Clock iso={j.finished_at} />
                    </TableCell>
                  </>
                )}
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
