import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { ChannelLink } from "@/components/job-row"
import { PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { VideoYouTubeLink } from "@/components/youtube-link"
import { recorderOf, usePods } from "@/hooks/use-pods"
import { api, type Job, type Pods } from "@/lib/api"
import { STEP_LABELS } from "@/lib/channel"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatBytes, formatClock, formatDuration, formatKst } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

function useRecordingNow() {
  return useQuery({
    queryKey: queryKeys.recordingNow(),
    queryFn: () => api.jobs({ kind: "record", state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
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
    job.started_at ? `started ${formatClock(job.started_at)}` : null,
    job.scheduled_at ? `scheduled ${formatClock(job.scheduled_at)}` : null,
    elapsed !== null ? `elapsed ${formatDuration(elapsed)}` : null,
  ].filter(Boolean)
  return <span className="text-xs text-muted-foreground tabular-nums">{parts.join(" · ")}</span>
}

// Kubernetes keeps the pod's state. The card speaks only when the recording may
// lose a stretch: no recorder Job, a pod not running, or a retry.
function PodAlert({ pods, jobId }: { pods: Pods | undefined; jobId: number }) {
  if (!pods?.available) return null
  const recorder = recorderOf(pods, jobId)
  if (!recorder) return <Badge variant="destructive">no recorder Job</Badge>
  if (recorder.pod_phase === "Running" && recorder.restarts === 0) return null
  return (
    <Badge variant="destructive" title={recorder.name}>
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
        <PodAlert pods={pods} jobId={job.id} />
      </div>
      <RecordingProgress job={job} now={now} />
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

export function ScheduledSection() {
  const queued = useQuery({
    queryKey: queryKeys.scheduledRecords(),
    queryFn: () => api.jobs({ kind: "record", state: ["queued"] }),
    refetchInterval: IDLE_REFETCH_MS,
  })
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
                </li>
              ))}
            </ul>
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
