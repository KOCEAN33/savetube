import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useSearchParams } from "react-router"

import { RunningJob } from "@/components/job-row"
import { JobsTable } from "@/components/jobs-table"
import { Page, PageSection, QueryState } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api, type Job, type JobState } from "@/lib/api"
import { JOB_PAGE, MAX_JOB_LIMIT } from "@/lib/constants"
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
  const [queuedShown, setQueuedShown] = useState(JOB_PAGE)
  const [recentLimit, setRecentLimit] = useState(JOB_PAGE)

  const active = useQuery({
    queryKey: queryKeys.allActiveJobs(),
    queryFn: () =>
      api.jobs({ kind: "archive", state: ["running", "queued"], limit: MAX_JOB_LIMIT }),
    refetchInterval: (q) => refetchMs(q.state.data?.some((j) => j.state === "running")),
  })
  const busy = active.data?.some((j) => j.state === "running") ?? false
  // The active list stops at MAX_JOB_LIMIT, so the queued count comes from the server.
  const counts = useQuery({
    queryKey: queryKeys.archiveJobCounts("all"),
    queryFn: () => api.jobCounts({ kind: "archive" }),
    refetchInterval: refetchMs(busy),
  })
  const recent = useQuery({
    queryKey: queryKeys.allRecentJobs({ state, unresolved, limit: recentLimit }),
    queryFn: () =>
      api.jobs({
        kind: "archive",
        state: state ? [state] : FINISHED,
        unresolved: unresolved || undefined,
        limit: recentLimit,
      }),
    placeholderData: (previous) => previous,
    refetchInterval: refetchMs(busy),
  })

  function update(key: string, value: string | undefined) {
    const next = new URLSearchParams(params)
    if (value === undefined) next.delete(key)
    else next.set(key, value)
    setParams(next)
    setRecentLimit(JOB_PAGE)
  }

  return (
    <Page>
      <QueryState query={active}>
        {(jobs) => {
          const running = jobs.filter((j) => j.state === "running")
          const queued = jobs.filter((j) => j.state === "queued").sort((a, b) => a.id - b.id)
          const queuedTotal = counts.data?.queued ?? queued.length
          return (
            <>
              <PageSection title="Downloading now">
                {running.length === 0 ? (
                  <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
                ) : (
                  <ul className="flex flex-col divide-y">
                    {running.map((j) => (
                      <RunningJob key={j.id} job={j} withChannel />
                    ))}
                  </ul>
                )}
              </PageSection>
              <PageSection title={`Queued (${queuedTotal}), next first`}>
                {queued.length === 0 ? (
                  <p className="p-3 text-sm text-muted-foreground">Nothing waits.</p>
                ) : (
                  <JobsTable
                    jobs={queued.slice(0, queuedShown)}
                    channelName={channelNames(queued)}
                    empty=""
                  />
                )}
                {queued.length > queuedShown && (
                  <div className="flex items-center gap-3 border-t p-3">
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
              </PageSection>
            </>
          )
        }}
      </QueryState>
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
          {(jobs) => (
            <>
              <JobsTable
                jobs={jobs}
                channelName={channelNames(jobs)}
                expandLog
                empty="No finished job."
              />
              {/* A full page means more may exist. */}
              {jobs.length >= recentLimit && recentLimit < MAX_JOB_LIMIT && (
                <div className="border-t p-3">
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
      </PageSection>
    </Page>
  )
}
