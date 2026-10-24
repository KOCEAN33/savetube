import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { ChannelLink } from "@/components/job-row"
import { PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { VideoYouTubeLink } from "@/components/youtube-link"
import { api, type Job, type Pods, type RecorderJob } from "@/lib/api"
import { STEP_LABELS } from "@/lib/channel"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatBytes, formatDateTime, formatDuration, formatKst } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

const UNAVAILABLE = "Pod state is unavailable: the web server runs outside a cluster."

function useRecordingNow() {
  return useQuery({
    queryKey: queryKeys.recordingNow(),
    queryFn: () => api.jobs({ kind: "record", state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
}

function usePods() {
  return useQuery({
    queryKey: queryKeys.pods(),
    queryFn: api.pods,
    refetchInterval: IDLE_REFETCH_MS,
  })
}

function recorderOf(pods: Pods | undefined, jobId: number): RecorderJob | undefined {
  return pods?.recorders.find((r) => r.job_id === jobId)
}

// A live has no total, so the line shows bytes and time since the start, not a
// percent. `now` is the fetch time, so the elapsed time matches the bytes.
function RecordingProgress({ job, now }: { job: Job; now: number }) {
  const p = job.progress
  const elapsed = job.started_at ? (now - Date.parse(job.started_at)) / 1000 : null
  const parts = [
    p && "step" in p ? STEP_LABELS[p.step] : null,
    p && "done" in p ? `${formatBytes(p.done)} so far` : "waiting for the first bytes",
    p && "speed" in p && p.speed !== null ? `${formatBytes(p.speed)}/s` : null,
    elapsed !== null ? `elapsed ${formatDuration(elapsed)}` : null,
  ].filter(Boolean)
  return <span className="text-xs text-muted-foreground tabular-nums">{parts.join(" · ")}</span>
}

function PodPhase({ pods, jobId }: { pods: Pods | undefined; jobId: number }) {
  if (!pods) return null
  if (!pods.available) return <span className="text-xs text-muted-foreground">pod unknown</span>
  const recorder = recorderOf(pods, jobId)
  if (!recorder) return <Badge variant="destructive">no recorder Job</Badge>
  return (
    <Badge variant="outline" title={recorder.name}>
      pod {recorder.pod_phase ?? "not started"}
      {recorder.restarts > 0 &&
        `, ${recorder.restarts} ${recorder.restarts === 1 ? "retry" : "retries"}`}
    </Badge>
  )
}

function RecordingRow({ job, pods, now }: { job: Job; pods: Pods | undefined; now: number }) {
  return (
    <li className="flex flex-col gap-1 p-3">
      <ChannelLink job={job} />
      <div className="flex flex-wrap items-baseline gap-2">
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
        {job.video_id && <VideoYouTubeLink id={job.video_id} title={job.title} />}
        <PodPhase pods={pods} jobId={job.id} />
      </div>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <RecordingProgress job={job} now={now} />
        <span className="text-xs text-muted-foreground">
          started {formatDateTime(job.started_at)}
        </span>
      </div>
    </li>
  )
}

export function RecordingNowSection() {
  const recording = useRecordingNow()
  const pods = usePods()
  return (
    <PageSection title="Recording now">
      <QueryState query={recording}>
        {(jobs) =>
          jobs.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">Nothing is recording.</p>
          ) : (
            <ul className="flex flex-col divide-y">
              {jobs.map((j) => (
                <RecordingRow key={j.id} job={j} pods={pods.data} now={recording.dataUpdatedAt} />
              ))}
            </ul>
          )
        }
      </QueryState>
    </PageSection>
  )
}

// A null scheduled_at is a stream found live, due now, so it sorts first.
function byStart(a: Job, b: Job) {
  return (a.scheduled_at ?? "").localeCompare(b.scheduled_at ?? "") || a.id - b.id
}

function RecorderCell({ pods, jobId }: { pods: Pods | undefined; jobId: number }) {
  if (!pods) return "-"
  if (!pods.available) return <span className="text-muted-foreground">unknown</span>
  const recorder = recorderOf(pods, jobId)
  if (!recorder) return <span className="text-muted-foreground">not yet</span>
  return `${recorder.status}, pod ${recorder.pod_phase ?? "not started"}`
}

export function ScheduledSection() {
  const queued = useQuery({
    queryKey: queryKeys.scheduledRecords(),
    queryFn: () => api.jobs({ kind: "record", state: ["queued"] }),
    refetchInterval: IDLE_REFETCH_MS,
  })
  const pods = usePods()
  return (
    <PageSection title="Scheduled">
      <QueryState query={queued}>
        {(jobs) =>
          jobs.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">No live is scheduled.</p>
          ) : (
            // A list, not a table: the record job table below stays the page's one table.
            <ul className="flex flex-col divide-y">
              {[...jobs].sort(byStart).map((j) => (
                <li key={j.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 p-3 text-sm">
                  <span className="w-44 shrink-0 tabular-nums">
                    {j.scheduled_at ? formatKst(j.scheduled_at) : "live now"}
                  </span>
                  {j.video_id ? (
                    <Link
                      className="min-w-0 flex-1 truncate font-medium hover:underline"
                      title={j.title ?? j.video_id}
                      to={`/videos/${encodeURIComponent(j.video_id)}`}
                    >
                      {j.title ?? j.video_id}
                    </Link>
                  ) : (
                    <span className="flex-1 font-medium">Job {j.id}</span>
                  )}
                  {j.video_id && <VideoYouTubeLink id={j.video_id} title={j.title} />}
                  <ChannelLink job={j} />
                  <span className="text-xs">
                    Recorder Job: <RecorderCell pods={pods.data} jobId={j.id} />
                  </span>
                </li>
              ))}
            </ul>
          )
        }
      </QueryState>
    </PageSection>
  )
}

export function PodsSection() {
  const pods = usePods()
  return (
    <PageSection title="Recorder and worker pods">
      <QueryState query={pods}>
        {(data) =>
          !data.available ? (
            <p className="p-3 text-sm text-muted-foreground">{UNAVAILABLE}</p>
          ) : (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Pod</TableHead>
                  <TableHead className="text-right">Restarts</TableHead>
                  <TableHead>Started</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.recorders.map((r) => (
                  <TableRow key={r.name}>
                    <TableCell>
                      {r.video_id ? (
                        <span className="inline-flex items-center gap-2">
                          <Link
                            className="hover:underline"
                            to={`/videos/${encodeURIComponent(r.video_id)}`}
                          >
                            {r.name}
                          </Link>
                          <VideoYouTubeLink id={r.video_id} iconOnly />
                        </span>
                      ) : (
                        r.name
                      )}
                    </TableCell>
                    <TableCell>{r.status}</TableCell>
                    <TableCell>{r.pod_phase ?? "-"}</TableCell>
                    <TableCell className="text-right">{r.restarts}</TableCell>
                    <TableCell>{formatDateTime(r.started_at)}</TableCell>
                  </TableRow>
                ))}
                {data.workers.map((w) => (
                  <TableRow key={w.name}>
                    <TableCell>{w.name}</TableCell>
                    <TableCell>{w.ready ? "ready" : "not ready"}</TableCell>
                    <TableCell>{w.phase ?? "-"}</TableCell>
                    <TableCell className="text-right">{w.restarts}</TableCell>
                    <TableCell>{formatDateTime(w.started_at)}</TableCell>
                  </TableRow>
                ))}
                {data.recorders.length + data.workers.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center text-muted-foreground">
                      No recorder Job or worker pod.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )
        }
      </QueryState>
    </PageSection>
  )
}

// Home's card: shown only while a live records.
export function RecordingCard() {
  const recording = useRecordingNow()
  const jobs = recording.data ?? []
  if (jobs.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border p-3 text-sm">
      <Badge variant="destructive">recording</Badge>
      <span className="font-medium">
        {jobs.length === 1 ? "1 live" : `${jobs.length} lives`} recording now:
      </span>
      <span className="min-w-0 truncate text-muted-foreground">
        {jobs.map((j) => j.title ?? j.video_id ?? `job ${j.id}`).join(", ")}
      </span>
      <Link className="ml-auto shrink-0 hover:underline" to="/live">
        Open Live
      </Link>
    </div>
  )
}
