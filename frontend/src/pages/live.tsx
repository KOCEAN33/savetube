import { useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router"

import { JobStateCounts, JobsTable } from "@/components/jobs-table"
import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api, type JobState } from "@/lib/api"
import { JOB_STATES } from "@/lib/channel"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"

const LIMIT = 200
const ALL = "all"

export function LivePage() {
  const [params, setParams] = useSearchParams()
  const state = (params.get("state") as JobState | null) ?? undefined
  const channels = useQuery({ queryKey: queryKeys.channels(), queryFn: api.channels })
  const jobs = useQuery({
    queryKey: queryKeys.recordJobs(state),
    queryFn: () => api.jobs({ kind: "record", state: state ? [state] : undefined, limit: LIMIT }),
    refetchInterval: IDLE_REFETCH_MS,
  })
  const channelName = new Map(channels.data?.map((c) => [c.id, c.name]))
  const watched = channels.data?.filter((c) => c.watch_lives) ?? []

  function setState(value: string) {
    const next = new URLSearchParams(params)
    if (value === ALL) next.delete("state")
    else next.set("state", value)
    setParams(next)
  }

  return (
    <Page>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Watching for lives:</span>
        {watched.length === 0 && <span>no channel. Turn on live recording on a channel page.</span>}
        {watched.map((c) => (
          <Badge key={c.id} variant="outline" asChild>
            <Link to={`/channels/${encodeURIComponent(c.id)}`}>{c.name}</Link>
          </Badge>
        ))}
      </div>
      <PageSection
        title="Detected, upcoming and recorded streams"
        actions={
          <Select value={state ?? ALL} onValueChange={setState}>
            <SelectTrigger size="sm" className="w-36" aria-label="State">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Any state</SelectItem>
              {JOB_STATES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      >
        <QueryState query={jobs}>
          {(rows) => (
            <>
              <JobStateCounts jobs={rows} />
              <JobsTable
                jobs={rows}
                channelName={channelName}
                scheduled
                empty="No live detected yet."
              />
            </>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
