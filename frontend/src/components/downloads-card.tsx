import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { ChannelLink, RunningJob } from "@/components/channel-jobs"
import { QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, type Job } from "@/lib/api"
import {
  IDLE_REFETCH_MS,
  JOB_PAGE,
  MAX_JOB_LIMIT,
  RUNNING_REFETCH_MS,
  jobStateVariant,
} from "@/lib/channel"
import { formatDateTime } from "@/lib/format"

function ProblemJob({ job }: { job: Job }) {
  return (
    <li className="flex flex-col gap-1 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <ChannelLink job={job} />
        <span className="text-xs text-muted-foreground">{formatDateTime(job.finished_at)}</span>
      </div>
      <div className="flex min-w-0 items-baseline gap-2">
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
      </div>
      {job.log_tail && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">{job.reason}</summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded bg-muted p-2 whitespace-pre-wrap break-all">
            {job.log_tail}
          </pre>
        </details>
      )}
    </li>
  )
}

// Archive downloads across every channel: what runs now, how many wait, and
// the latest failed and interrupted runs with their reason.
export function DownloadsCard() {
  const [problemLimit, setProblemLimit] = useState(JOB_PAGE)
  const active = useQuery({
    queryKey: ["jobs", "archive", "all", "active"],
    queryFn: () =>
      api.jobs({ kind: "archive", state: ["running", "queued"], limit: MAX_JOB_LIMIT }),
    refetchInterval: (q) =>
      q.state.data?.some((j) => j.state === "running") ? RUNNING_REFETCH_MS : IDLE_REFETCH_MS,
  })
  const busy = active.data?.some((j) => j.state === "running") ?? false
  const problems = useQuery({
    queryKey: ["jobs", "archive", "all", "problems", problemLimit],
    queryFn: () =>
      api.jobs({ kind: "archive", state: ["failed", "interrupted"], limit: problemLimit }),
    placeholderData: (previous) => previous,
    refetchInterval: busy ? RUNNING_REFETCH_MS : IDLE_REFETCH_MS,
  })

  const running = active.data?.filter((j) => j.state === "running") ?? []
  const queued = active.data?.filter((j) => j.state === "queued").length ?? 0
  // The list stops at MAX_JOB_LIMIT jobs, so a full list may hide more.
  const queuedLabel = active.data?.length === MAX_JOB_LIMIT ? `${queued}+` : `${queued}`

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Downloads</CardTitle>
        <CardDescription>
          {active.data ? `${running.length} running, ${queuedLabel} queued` : "Loading"}
        </CardDescription>
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
        <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">
          Recently failed and interrupted
        </h3>
        <QueryState query={problems}>
          {(jobs) => (
            <>
              {jobs.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">No failed or interrupted job.</p>
              ) : (
                <ul className="flex flex-col divide-y">
                  {jobs.map((j) => (
                    <ProblemJob key={j.id} job={j} />
                  ))}
                </ul>
              )}
              {/* A full page means more may exist. */}
              {jobs.length >= problemLimit && problemLimit < MAX_JOB_LIMIT && (
                <div className="border-t p-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setProblemLimit(Math.min(problemLimit + JOB_PAGE, MAX_JOB_LIMIT))
                    }
                  >
                    Show more
                  </Button>
                </div>
              )}
            </>
          )}
        </QueryState>
      </CardContent>
    </Card>
  )
}
