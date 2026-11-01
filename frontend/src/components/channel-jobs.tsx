import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { RunningJob } from "@/components/job-row"
import { JobsTable } from "@/components/jobs-table"
import { Pagination } from "@/components/pagination"
import { QueryState } from "@/components/query-state"
import { usePageParam } from "@/hooks/use-page-param"
import { api } from "@/lib/api"
import { JOB_PAGE } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

// The running download first, then the queued jobs in the order the
// worker takes them, then the latest finished jobs.
export function ChannelJobs({ channelId }: { channelId: string }) {
  const [queuedPage, setQueuedPage] = usePageParam("qpage")
  const [recentPage, setRecentPage] = usePageParam("fpage")
  const running = useQuery({
    queryKey: queryKeys.runningJobs(channelId),
    queryFn: () => api.jobs({ kind: "archive", channel_id: channelId, state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
  const busy = (running.data?.length ?? 0) > 0
  const queued = useQuery({
    queryKey: queryKeys.queuedJobs(channelId, queuedPage),
    queryFn: () =>
      api.jobPage({
        kind: "archive",
        channel_id: channelId,
        state: ["queued"],
        order: "dispatch",
        limit: JOB_PAGE,
        offset: (queuedPage - 1) * JOB_PAGE,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: refetchMs(busy),
  })
  const recent = useQuery({
    queryKey: queryKeys.channelRecentJobs(channelId, recentPage),
    queryFn: () =>
      api.jobPage({
        kind: "archive",
        channel_id: channelId,
        // An interrupted run is a row of its own. Its job waits in the queue again.
        state: ["succeeded", "failed", "cancelled", "interrupted"],
        limit: JOB_PAGE,
        offset: (recentPage - 1) * JOB_PAGE,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: refetchMs(busy),
  })

  return (
    <div className="flex flex-col">
      <h3 className="border-b bg-muted px-3 py-2 text-sm font-medium">Downloading now</h3>
      <QueryState query={running}>
        {(jobs) =>
          jobs.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
          ) : (
            <ul className="flex flex-col divide-y">
              {jobs.map((j) => (
                <RunningJob key={j.id} job={j} />
              ))}
            </ul>
          )
        }
      </QueryState>
      <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">
        Queued{queued.data ? ` (${queued.data.total})` : ""}, next first
      </h3>
      <QueryState query={queued}>
        {(page) => (
          <>
            {page.total === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">Nothing waits.</p>
            ) : (
              <JobsTable jobs={page.items} empty="" />
            )}
            <Pagination
              page={queuedPage}
              total={page.total}
              pageSize={JOB_PAGE}
              onPage={setQueuedPage}
              label="Queued pages"
            />
          </>
        )}
      </QueryState>
      <h3 className="border-y bg-muted px-3 py-2 text-sm font-medium">Recently finished</h3>
      <QueryState query={recent}>
        {(page) => (
          <>
            <JobsTable jobs={page.items} empty="No finished job yet." />
            <Pagination
              page={recentPage}
              total={page.total}
              pageSize={JOB_PAGE}
              onPage={setRecentPage}
              label="Finished pages"
            />
          </>
        )}
      </QueryState>
    </div>
  )
}
