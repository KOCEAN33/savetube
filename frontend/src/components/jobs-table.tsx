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
import type { Job, JobState, MissReason } from "@/lib/api"
import { JOB_STATES } from "@/lib/channel"
import { formatClock, formatDateTime } from "@/lib/format"

const MISS_REASON_LABEL: Record<MissReason, string> = {
  dvr_off: "DVR off",
  ended_before_detection: "ended before detection",
  recorder_failed: "recorder failed",
}

function stateVariant(state: JobState) {
  if (state === "failed" || state === "missed") return "destructive" as const
  if (state === "succeeded") return "secondary" as const
  return "outline" as const
}

export function JobStateCounts({ jobs }: { jobs: Job[] }) {
  const counts = new Map<JobState, number>()
  for (const job of jobs) counts.set(job.state, (counts.get(job.state) ?? 0) + 1)
  return (
    <div className="flex flex-wrap gap-2 border-b p-3 text-sm">
      <span className="text-muted-foreground">Last {jobs.length} job(s):</span>
      {JOB_STATES.filter((s) => counts.has(s)).map((s) => (
        <Badge key={s} variant={stateVariant(s)}>
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
  empty,
}: {
  jobs: Job[]
  // Set to show a channel column.
  channelName?: Map<string, string>
  // Show the announced start, which only a recording has.
  scheduled?: boolean
  empty: string
}) {
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
        {jobs.map((j) => (
          <TableRow key={j.id}>
            {/* w-full with max-w-0 gives this column the leftover width and truncates inside it. */}
            <TableCell className="w-full max-w-0">
              {j.video_id ? (
                <Link
                  className="block truncate hover:underline"
                  title={j.title ?? j.video_id}
                  to={`/videos/${encodeURIComponent(j.video_id)}`}
                >
                  {j.title ?? j.video_id}
                </Link>
              ) : (
                "-"
              )}
            </TableCell>
            {channelName && (
              <TableCell>
                {j.channel_id ? (
                  <Link
                    className="hover:underline"
                    to={`/channels/${encodeURIComponent(j.channel_id)}`}
                  >
                    {channelName.get(j.channel_id) ?? j.channel_id}
                  </Link>
                ) : (
                  "-"
                )}
              </TableCell>
            )}
            <TableCell>
              <Badge variant={stateVariant(j.state)}>{j.state}</Badge>
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
            </TableCell>
            <TableCell className="text-right">{j.attempts}</TableCell>
            {scheduled && <TableCell>{formatDateTime(j.scheduled_at)}</TableCell>}
            <TableCell>{formatDateTime(j.created_at)}</TableCell>
            <TableCell>{formatDateTime(j.started_at)}</TableCell>
            <TableCell>{formatDateTime(j.finished_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
