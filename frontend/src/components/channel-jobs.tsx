import { useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { RunningJob } from "@/components/job-row"
import { JobsTable } from "@/components/jobs-table"
import { QueryState } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { JOB_PAGE, MAX_JOB_LIMIT } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

// The running download first, then the queued jobs in the order the
// dispatcher takes them (oldest first), then the latest finished jobs.
export function ChannelJobs({ channelId }: { channelId: string }) {
  const [queuedShown, setQueuedShown] = useState(JOB_PAGE)
  const [recentLimit, setRecentLimit] = useState(JOB_PAGE)
  const active = useQuery({
    queryKey: queryKeys.channelActiveJobs(channelId),
    queryFn: () =>
      api.jobs({
        kind: "archive",
        channel_id: channelId,
        state: ["running", "queued"],
        limit: MAX_JOB_LIMIT,
      }),
    refetchInterval: (q) => refetchMs(q.state.data?.some((j) => j.state === "running")),
  })
  const busy = active.data?.some((j) => j.state === "running") ?? false
  // The active list stops at MAX_JOB_LIMIT, so the queued counts come from the server.
  const counts = useQuery({
    queryKey: queryKeys.archiveJobCounts(channelId),
    queryFn: () => api.jobCounts({ kind: "archive", channel_id: channelId }),
    refetchInterval: refetchMs(busy),
  })
  const recent = useQuery({
    queryKey: queryKeys.channelRecentJobs(channelId, recentLimit),
    queryFn: () =>
      api.jobs({
        kind: "archive",
        channel_id: channelId,
        // An interrupted run is a row of its own. Its job waits in the queue again.
        state: ["succeeded", "failed", "cancelled", "interrupted"],
        limit: recentLimit,
      }),
    placeholderData: (previous) => previous,
    refetchInterval: refetchMs(busy),
  })

  return (
    <div className="flex flex-col">
      <QueryState query={active}>
        {(jobs) => {
          const running = jobs.filter((j) => j.state === "running")
          const queued = jobs.filter((j) => j.state === "queued").sort((a, b) => a.id - b.id)
          const queuedTotal = counts.data?.queued ?? queued.length
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
                Queued ({queuedTotal}), next first
              </h3>
              {queued.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">Nothing waits.</p>
              ) : (
                <JobsTable jobs={queued.slice(0, queuedShown)} empty="" />
              )}
              {queued.length > queuedShown && (
                <div className="flex items-center gap-3 p-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setQueuedShown(queuedShown + JOB_PAGE)}
                  >
                    Show more
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    {queuedTotal - queuedShown} more wait after these.
                  </span>
                </div>
              )}
            </>
          )
        }}
      </QueryState>
      <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">Recently finished</h3>
      <QueryState query={recent}>
        {(jobs) => (
          <>
            <JobsTable jobs={jobs} empty="No finished job yet." />
            {/* A full page means more may exist. */}
            {jobs.length >= recentLimit && recentLimit < MAX_JOB_LIMIT && (
              <div className="p-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setRecentLimit(Math.min(recentLimit + JOB_PAGE, MAX_JOB_LIMIT))}
                >
                  Show more
                </Button>
              </div>
            )}
          </>
        )}
      </QueryState>
    </div>
  )
}
