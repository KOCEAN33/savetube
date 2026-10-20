import { useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router"

import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, type Job, type JobKind, type JobState, type MissReason } from "@/lib/api"
import { formatClock, formatDateTime } from "@/lib/format"

const LIMIT = 200
const ALL = "all"
const STATES: JobState[] = ["queued", "running", "succeeded", "failed", "missed", "cancelled"]

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

function StateCounts({ jobs }: { jobs: Job[] }) {
  const counts = new Map<JobState, number>()
  for (const job of jobs) counts.set(job.state, (counts.get(job.state) ?? 0) + 1)
  return (
    <div className="flex flex-wrap gap-2 border-b p-3 text-sm">
      <span className="text-muted-foreground">
        Last {jobs.length} job(s):
      </span>
      {STATES.filter((s) => counts.has(s)).map((s) => (
        <Badge key={s} variant={stateVariant(s)}>
          {s} {counts.get(s)}
        </Badge>
      ))}
    </div>
  )
}

export function JobsPage() {
  const [params, setParams] = useSearchParams()
  const kind = (params.get("kind") as JobKind | null) ?? undefined
  const state = (params.get("state") as JobState | null) ?? undefined

  const jobs = useQuery({
    queryKey: ["jobs", kind, state],
    queryFn: () => api.jobs({ kind, state, limit: LIMIT }),
    refetchInterval: 15_000,
  })

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value === ALL) next.delete(key)
    else next.set(key, value)
    setParams(next)
  }

  const filters = (
    <div className="flex items-center gap-2">
      <Tabs value={kind ?? ALL} onValueChange={(v) => setParam("kind", v)}>
        <TabsList>
          <TabsTrigger value={ALL}>All</TabsTrigger>
          <TabsTrigger value="archive">Archive</TabsTrigger>
          <TabsTrigger value="record">Live recordings</TabsTrigger>
        </TabsList>
      </Tabs>
      <Select value={state ?? ALL} onValueChange={(v) => setParam("state", v)}>
        <SelectTrigger size="sm" className="w-36" aria-label="State">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>Any state</SelectItem>
          {STATES.map((s) => (
            <SelectItem key={s} value={s}>
              {s}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )

  return (
    <Page>
      <PageSection title="Jobs" actions={filters}>
        <QueryState query={jobs}>
          {(rows) => (
            <>
              <StateCounts jobs={rows} />
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>Kind</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Video</TableHead>
                    <TableHead className="text-right">Attempts</TableHead>
                    <TableHead>Scheduled start</TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead>Started</TableHead>
                    <TableHead>Finished</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((j) => (
                    <TableRow key={j.id}>
                      <TableCell>{j.id}</TableCell>
                      <TableCell>{j.kind === "record" ? "live recording" : "archive"}</TableCell>
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
                      <TableCell className="font-mono text-xs">
                        {j.video_id ? (
                          <Link className="hover:underline" to={`/videos/${encodeURIComponent(j.video_id)}`}>
                            {j.video_id}
                          </Link>
                        ) : (
                          "-"
                        )}
                      </TableCell>
                      <TableCell className="text-right">{j.attempts}</TableCell>
                      <TableCell>{formatDateTime(j.scheduled_at)}</TableCell>
                      <TableCell>{formatDateTime(j.created_at)}</TableCell>
                      <TableCell>{formatDateTime(j.started_at)}</TableCell>
                      <TableCell>{formatDateTime(j.finished_at)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
