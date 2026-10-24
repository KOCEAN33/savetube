import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"

import { RunningJob } from "@/components/job-row"
import { ErrorBox, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { VideoYouTubeLink } from "@/components/youtube-link"
import { api, mutations, type Job } from "@/lib/api"
import { jobStateVariant } from "@/lib/channel"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatBytes, formatDateTime } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

const PROBLEM_ROWS = 3

// One line per run. Hovering the reason shows the full log_tail.
function ProblemJob({ job }: { job: Job }) {
  return (
    <li className="flex min-w-0 items-baseline gap-2 px-3 py-2 text-sm">
      <Badge variant={jobStateVariant(job.state)}>{job.state}</Badge>
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
      {job.video_id && <VideoYouTubeLink id={job.video_id} title={job.title} iconOnly />}
      {job.reason && (
        <span
          className="min-w-0 truncate text-xs text-muted-foreground"
          title={job.log_tail ?? undefined}
        >
          {job.reason}
        </span>
      )}
      <span className="ml-auto shrink-0 text-xs text-muted-foreground">
        {formatDateTime(job.finished_at)}
      </span>
    </li>
  )
}

// Finished videos waiting on staging, and Move now, which the scheduler takes
// on its next check (docs/plan/storage.md, Staging).
function StagingLine() {
  const queryClient = useQueryClient()
  const staged = useQuery({
    queryKey: queryKeys.staging(),
    queryFn: api.staging,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const move = useMutation({
    mutationFn: mutations.moveStaged,
    onSuccess: (updated) => queryClient.setQueryData(queryKeys.staging(), updated),
  })
  if (!staged.data || staged.data.count === 0) return null
  const { count, bytes, move_requested } = staged.data
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
      <span>
        {`${count} finished, waiting to move into the archive (${formatBytes(bytes)}).`}
        {move_requested && " Move requested, it starts within a minute."}
      </span>
      <Button
        size="sm"
        variant="outline"
        disabled={move_requested || move.isPending}
        onClick={() => move.mutate()}
      >
        Move now
      </Button>
      {move.error && <ErrorBox error={move.error} />}
    </div>
  )
}

// Archive downloads across every channel: what runs now, how many wait, and
// the latest failed and interrupted runs no later run fixed. /downloads lists the rest.
export function DownloadsCard() {
  const active = useQuery({
    queryKey: queryKeys.runningJobs(),
    queryFn: () => api.jobs({ kind: "archive", state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
  const busy = (active.data?.length ?? 0) > 0
  const counts = useQuery({
    queryKey: queryKeys.archiveJobCounts("all"),
    queryFn: () => api.jobCounts({ kind: "archive" }),
    refetchInterval: refetchMs(busy),
  })
  const problems = useQuery({
    queryKey: queryKeys.problemJobs(),
    queryFn: () =>
      api.jobs({
        kind: "archive",
        state: ["failed", "interrupted"],
        unresolved: true,
        limit: PROBLEM_ROWS,
      }),
    refetchInterval: refetchMs(busy),
  })

  const running = active.data ?? []

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Downloads</CardTitle>
        <CardDescription>
          {counts.data
            ? `${counts.data.running} running, ${counts.data.queued} queued`
            : "Loading"}
        </CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" asChild>
            <Link to="/downloads">View all</Link>
          </Button>
        </CardAction>
        <StagingLine />
      </CardHeader>
      <CardContent className="px-0">
        <QueryState query={active}>
          {() =>
            running.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
            ) : (
              <ul className="flex flex-col divide-y">
                {running.map((j) => (
                  <RunningJob key={j.id} job={j} withChannel />
                ))}
              </ul>
            )
          }
        </QueryState>
        <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">Unresolved failures</h3>
        <QueryState query={problems}>
          {(jobs) =>
            jobs.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">No unresolved failure.</p>
            ) : (
              <ul className="flex flex-col divide-y">
                {jobs.map((j) => (
                  <ProblemJob key={j.id} job={j} />
                ))}
              </ul>
            )
          }
        </QueryState>
      </CardContent>
    </Card>
  )
}
