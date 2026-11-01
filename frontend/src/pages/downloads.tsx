import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { useSearchParams } from "react-router"

import { RunningJob } from "@/components/job-row"
import { JobsTable } from "@/components/jobs-table"
import { Pagination } from "@/components/pagination"
import { Page, PageSection, QueryState } from "@/components/query-state"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { usePageParam } from "@/hooks/use-page-param"
import { api, type Job, type JobState } from "@/lib/api"
import { JOB_PAGE } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

const ALL = "all"
// An interrupted run is a row of its own. Its job waits in the queue again.
const FINISHED: JobState[] = ["succeeded", "failed", "interrupted", "cancelled"]

// Each job carries its uploader's name, which covers channels that are not tracked.
function channelNames(jobs: Job[]) {
  const names = new Map<string, string>()
  for (const j of jobs) if (j.channel_id && j.channel_name) names.set(j.channel_id, j.channel_name)
  return names
}

// Archive downloads across every channel, in the channel page's layout: the
// running downloads, the queue in dispatch order, then the finished runs.
export function DownloadsPage() {
  const [params, setParams] = useSearchParams()
  const stateParam = params.get("state") as JobState | null
  const state = stateParam && FINISHED.includes(stateParam) ? stateParam : undefined
  // Hiding resolved failures is the default, so only showing them takes a param.
  const unresolved = params.get("resolved") !== "show"
  const [queuedPage, setQueuedPage] = usePageParam("qpage")
  const [recentPage, setRecentPage] = usePageParam("fpage")

  const running = useQuery({
    queryKey: queryKeys.runningJobs(),
    queryFn: () => api.jobs({ kind: "archive", state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
  const busy = (running.data?.length ?? 0) > 0
  const queued = useQuery({
    queryKey: queryKeys.queuedJobs("all", queuedPage),
    queryFn: () =>
      api.jobPage({
        kind: "archive",
        state: ["queued"],
        order: "dispatch",
        limit: JOB_PAGE,
        offset: (queuedPage - 1) * JOB_PAGE,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: refetchMs(busy),
  })
  const recent = useQuery({
    queryKey: queryKeys.allRecentJobs({ state, unresolved, page: recentPage }),
    queryFn: () =>
      api.jobPage({
        kind: "archive",
        state: state ? [state] : FINISHED,
        unresolved: unresolved || undefined,
        limit: JOB_PAGE,
        offset: (recentPage - 1) * JOB_PAGE,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: refetchMs(busy),
  })

  // A filter change starts the finished list over at its first page.
  function update(key: string, value: string | undefined) {
    const next = new URLSearchParams(params)
    if (value === undefined) next.delete(key)
    else next.set(key, value)
    next.delete("fpage")
    setParams(next)
  }

  return (
    <Page>
      <PageSection title="Downloading now">
        <QueryState query={running}>
          {(jobs) =>
            jobs.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
            ) : (
              <ul className="flex flex-col divide-y">
                {jobs.map((j) => (
                  <RunningJob key={j.id} job={j} withChannel />
                ))}
              </ul>
            )
          }
        </QueryState>
      </PageSection>
      <PageSection
        title={`Queued${queued.data ? ` (${queued.data.total})` : ""}, next first`}
      >
        <QueryState query={queued}>
          {(page) => (
            <>
              {page.total === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">Nothing waits.</p>
              ) : (
                <JobsTable jobs={page.items} channelName={channelNames(page.items)} empty="" />
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
      </PageSection>
      <PageSection
        title="Finished"
        actions={
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <Checkbox
                id="downloads-hide-resolved"
                checked={unresolved}
                onCheckedChange={(on) => update("resolved", on === true ? undefined : "show")}
              />
              <Label htmlFor="downloads-hide-resolved">Hide resolved</Label>
            </div>
            <Select
              value={state ?? ALL}
              onValueChange={(v) => update("state", v === ALL ? undefined : v)}
            >
              <SelectTrigger size="sm" className="w-36" aria-label="State">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All</SelectItem>
                {FINISHED.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        }
      >
        <QueryState query={recent}>
          {(page) => (
            <>
              <JobsTable
                jobs={page.items}
                channelName={channelNames(page.items)}
                empty="No finished job."
              />
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
      </PageSection>
    </Page>
  )
}
