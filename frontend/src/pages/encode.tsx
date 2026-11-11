import { Fragment, useState, type FormEvent, type MouseEvent } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"

import { Clock } from "@/components/clock"
import { Pagination } from "@/components/pagination"
import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { WordListEditor } from "@/components/source-words"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useDismiss } from "@/hooks/use-dismiss"
import { usePageParam } from "@/hooks/use-page-param"
import {
  api,
  mutations,
  type Encode,
  type EncodeJob,
  type EncodePages,
  type EncodeSettingsIn,
  type EncodeState,
  type EncodeSummary,
  type EncodeTune,
} from "@/lib/api"
import { IDLE_REFETCH_MS, JOB_PAGE } from "@/lib/constants"
import { formatBytes, formatCount } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"

function encodeStateVariant(state: EncodeState) {
  if (state === "failed") return "destructive" as const
  if (state === "lost") return "warning" as const
  if (state === "succeeded") return "secondary" as const
  return "outline" as const
}

const TUNES: EncodeTune[] = ["hq", "uhq"]

interface OptionsForm {
  tune: EncodeTune
  cq: string
  // Empty keeps the source's frame rate.
  fps_cap: string
  still_fps_cap: string
  // Empty applies the FPS cap without measuring.
  motion_threshold: string
  still_words: string[]
  still_exclude_words: string[]
}

function toForm(e: Encode): OptionsForm {
  const cap = (v: number | null) => (v === null ? "" : String(v))
  return {
    tune: e.tune,
    cq: String(e.cq),
    fps_cap: cap(e.fps_cap),
    still_fps_cap: cap(e.still_fps_cap),
    motion_threshold: cap(e.motion_threshold),
    still_words: e.still_words,
    still_exclude_words: e.still_exclude_words,
  }
}

// A positive number field's value, null for empty, undefined when invalid.
function parseCap(value: string): number | null | undefined {
  if (value.trim() === "") return null
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : undefined
}

// The options as the backend takes them, or the message for the first rule the form breaks.
function toOptions(f: OptionsForm): Omit<EncodeSettingsIn, "enabled"> | string {
  const cq = Number(f.cq)
  if (f.cq.trim() === "" || !Number.isInteger(cq) || cq < 1 || cq > 63)
    return "CQ must be a whole number from 1 to 63."
  const fps_cap = parseCap(f.fps_cap)
  if (fps_cap === undefined) return "FPS cap must be a positive number, or empty."
  const still_fps_cap = parseCap(f.still_fps_cap)
  if (still_fps_cap === undefined) return "Still FPS cap must be a positive number, or empty."
  const motion_threshold = parseCap(f.motion_threshold)
  if (motion_threshold === undefined)
    return "Motion threshold must be a positive number, or empty."
  return {
    tune: f.tune,
    cq,
    fps_cap,
    still_fps_cap,
    motion_threshold,
    still_words: f.still_words,
    still_exclude_words: f.still_exclude_words,
  }
}

function settingsOf(e: Encode): EncodeSettingsIn {
  return {
    enabled: e.enabled,
    tune: e.tune,
    cq: e.cq,
    fps_cap: e.fps_cap,
    still_fps_cap: e.still_fps_cap,
    motion_threshold: e.motion_threshold,
    still_words: e.still_words,
    still_exclude_words: e.still_exclude_words,
  }
}

// The switch saves on change, like the download pause. The options save with
// the button, so a half-typed number never queues a job.
function EncodeSettingsForm({
  encode,
  save,
}: {
  encode: Encode
  save: {
    isPending: boolean
    isSuccess: boolean
    mutate: (body: EncodeSettingsIn) => void
  }
}) {
  const [form, setForm] = useState(() => toForm(encode))
  const [invalid, setInvalid] = useState<string | null>(null)

  function submit(e: FormEvent) {
    e.preventDefault()
    const options = toOptions(form)
    if (typeof options === "string") {
      setInvalid(options)
      return
    }
    setInvalid(null)
    save.mutate({ enabled: encode.enabled, ...options })
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <p className="text-sm text-muted-foreground">
        Queues stored H.264 and VP9 videos for the external AV1 encoder.
      </p>
      <div className="flex items-center gap-2">
        <Switch
          id="encode-enabled"
          checked={encode.enabled}
          disabled={save.isPending}
          onCheckedChange={(enabled) => save.mutate({ ...settingsOf(encode), enabled })}
        />
        <Label htmlFor="encode-enabled">Re-encode</Label>
      </div>
      <form onSubmit={submit} className="flex max-w-xl flex-col gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="encode-tune">Tune</Label>
            <Select
              value={form.tune}
              onValueChange={(tune) => setForm({ ...form, tune: tune as EncodeTune })}
            >
              <SelectTrigger id="encode-tune" className="w-24">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TUNES.map((t) => (
                  <SelectItem key={t} value={t}>
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="encode-cq">CQ</Label>
            <Input
              id="encode-cq"
              type="number"
              min={1}
              max={63}
              step={1}
              required
              className="w-24"
              value={form.cq}
              onChange={(e) => setForm({ ...form, cq: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="encode-fps-cap">FPS cap</Label>
            <Input
              id="encode-fps-cap"
              type="number"
              min={0}
              step="any"
              placeholder="source"
              className="w-28"
              value={form.fps_cap}
              onChange={(e) => setForm({ ...form, fps_cap: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="encode-still-fps-cap">Still FPS cap</Label>
            <Input
              id="encode-still-fps-cap"
              type="number"
              min={0}
              step="any"
              placeholder="source"
              className="w-28"
              value={form.still_fps_cap}
              onChange={(e) => setForm({ ...form, still_fps_cap: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="encode-motion-threshold">Motion threshold</Label>
            <Input
              id="encode-motion-threshold"
              type="number"
              min={0}
              step="any"
              placeholder="off"
              className="w-28"
              value={form.motion_threshold}
              onChange={(e) => setForm({ ...form, motion_threshold: e.target.value })}
            />
          </div>
        </div>
        <WordListEditor
          id="encode-still-words"
          label="Still words"
          hint="A title that contains any of these takes the still FPS cap."
          words={form.still_words}
          onChange={(still_words) => setForm({ ...form, still_words })}
        />
        <WordListEditor
          id="encode-still-exclude-words"
          label="Still exclude words"
          hint="A title that contains any of these never counts as still."
          words={form.still_exclude_words}
          onChange={(still_exclude_words) => setForm({ ...form, still_exclude_words })}
        />
        {invalid && <ErrorBox error={new Error(invalid)} />}
        <div className="flex items-center gap-2">
          <Button type="submit" disabled={save.isPending}>
            Save
          </Button>
          {save.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
        </div>
      </form>
    </div>
  )
}

function Stat({ label, value, detail }: { label: string; value: string; detail?: string[] }) {
  return (
    <div className="flex flex-col gap-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-2xl font-semibold tabular-nums">{value}</dd>
      {detail?.map((line) => (
        <dd key={line} className="text-xs text-muted-foreground tabular-nums">
          {line}
        </dd>
      ))}
    </div>
  )
}

function SummaryStats({ summary: s }: { summary: EncodeSummary }) {
  const percent = s.source_bytes > 0 ? Math.round((s.saved_bytes / s.source_bytes) * 100) : null
  return (
    <dl className="grid grid-cols-2 gap-4 p-4 md:grid-cols-4">
      <Stat
        label="Space saved"
        value={formatBytes(s.saved_bytes)}
        detail={[
          percent === null ? "No result yet" : `${percent}% of ${formatBytes(s.source_bytes)}`,
          `${formatBytes(s.held_bytes)} held until originals expire`,
        ]}
      />
      <Stat label="Succeeded" value={formatCount(s.succeeded)} />
      <Stat label="Failed" value={formatCount(s.failed)} />
      <Stat label="Queued or claimed" value={formatCount(s.open)} />
    </dl>
  )
}

function EncodeJobsTable({
  jobs,
  empty,
  busy,
  onCancel,
  onRequeue,
  onDismiss,
}: {
  jobs: EncodeJob[]
  empty: string
  busy: boolean
  onCancel: (id: number) => void
  onRequeue: (id: number) => void
  onDismiss: (id: number) => void
}) {
  const [open, setOpen] = useState<Set<number>>(new Set())
  const columns = 6
  return (
    <Table className="min-w-4xl table-fixed">
      <TableHeader className="bg-muted">
        <TableRow>
          <TableHead>Video</TableHead>
          <TableHead className="w-40">State</TableHead>
          <TableHead className="w-48">Size</TableHead>
          <TableHead className="w-28">Queued</TableHead>
          <TableHead className="w-28">Finished</TableHead>
          <TableHead className="w-48" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {jobs.length === 0 && (
          <TableRow>
            <TableCell colSpan={columns} className="text-center text-muted-foreground">
              {empty}
            </TableCell>
          </TableRow>
        )}
        {jobs.map((j) => {
          // A click on a failed row opens its reason and log_tail below it.
          const expandable = j.state === "failed" && j.reason !== null
          const expanded = expandable && open.has(j.id)
          function toggle(e: MouseEvent) {
            if ((e.target as HTMLElement).closest("a, button")) return
            const next = new Set(open)
            if (expanded) next.delete(j.id)
            else next.add(j.id)
            setOpen(next)
          }
          return (
            <Fragment key={j.id}>
              <TableRow
                className={expandable ? "cursor-pointer" : undefined}
                onClick={expandable ? toggle : undefined}
              >
                <TableCell className="w-full max-w-0 min-w-48">
                  <Link
                    className="block truncate hover:underline"
                    title={j.title}
                    to={`/videos/${encodeURIComponent(j.video_id)}`}
                  >
                    {j.title}
                  </Link>
                </TableCell>
                <TableCell>
                  <Badge
                    variant={encodeStateVariant(j.state)}
                    aria-expanded={expandable ? expanded : undefined}
                    title={j.reason ?? undefined}
                  >
                    {j.state}
                  </Badge>
                  {j.dismissed && (
                    <span className="ml-2 text-xs text-muted-foreground">dismissed</span>
                  )}
                </TableCell>
                <TableCell className="truncate tabular-nums">
                  {formatBytes(j.source_size)} &rarr; {formatBytes(j.result_size)}
                </TableCell>
                <TableCell className="truncate tabular-nums">
                  <Clock iso={j.created_at} />
                </TableCell>
                <TableCell className="truncate tabular-nums">
                  <Clock iso={j.finished_at} />
                </TableCell>
                <TableCell className="text-right">
                  {j.state === "queued" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => onCancel(j.id)}
                    >
                      Cancel
                    </Button>
                  )}
                  {j.state === "failed" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => onRequeue(j.id)}
                    >
                      Requeue
                    </Button>
                  )}
                  {j.state === "failed" && !j.dismissed && (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-2"
                      disabled={busy}
                      onClick={() => onDismiss(j.id)}
                    >
                      Dismiss
                    </Button>
                  )}
                </TableCell>
              </TableRow>
              {expanded && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={columns} className="max-w-0 whitespace-normal">
                    <p className="mb-1 text-sm font-medium">{j.reason}</p>
                    {j.log_tail && (
                      <pre className="max-h-60 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap break-all">
                        {j.log_tail}
                      </pre>
                    )}
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}

// Re-encoding on the external encoder (docs/plan/encode.md): the settings, the
// savings, the jobs that wait on the encoder or on a user, then the finished ones.
export function EncodePage() {
  const queryClient = useQueryClient()
  const [waitingPage, setWaitingPage] = usePageParam("qpage")
  const [finishedPage, setFinishedPage] = usePageParam("fpage")
  const pages: EncodePages = {
    open_offset: (waitingPage - 1) * JOB_PAGE,
    finished_offset: (finishedPage - 1) * JOB_PAGE,
    limit: JOB_PAGE,
  }
  const key = queryKeys.encode(waitingPage, finishedPage)
  // The interval shows jobs the scheduler and the encoder move.
  const encode = useQuery({
    queryKey: key,
    queryFn: () => api.encode(pages),
    placeholderData: keepPreviousData,
    refetchInterval: IDLE_REFETCH_MS,
  })
  // Every route answers with the pages shown, so the answer replaces them.
  const onSuccess = (updated: Encode) => queryClient.setQueryData(key, updated)
  const save = useMutation({
    mutationFn: (body: EncodeSettingsIn) => mutations.replaceEncodeSettings(body, pages),
    onSuccess,
  })
  const cancel = useMutation({
    mutationFn: (id: number) => mutations.cancelEncodeJob(id, pages),
    onSuccess,
  })
  const requeue = useMutation({
    mutationFn: (id: number) => mutations.requeueEncodeJob(id, pages),
    onSuccess,
  })
  // A dismissed failure moves to Finished and its video stays unqueued.
  const dismiss = useDismiss()
  const onDismiss = (id: number) => dismiss.mutate([{ kind: "encode_failed", key: String(id) }])
  const busy = cancel.isPending || requeue.isPending || dismiss.isPending
  const errors = [save, cancel, requeue, dismiss].flatMap((m) => (m.error ? [m.error] : []))

  return (
    <Page>
      <PageSection title="Re-encoding">
        <QueryState query={encode}>
          {(data) => <EncodeSettingsForm encode={data} save={save} />}
        </QueryState>
      </PageSection>
      {errors.map((e) => (
        <ErrorBox key={e.message} error={e} />
      ))}
      <PageSection title="Summary">
        <QueryState query={encode}>{(data) => <SummaryStats summary={data.summary} />}</QueryState>
      </PageSection>
      <PageSection
        title={`Waiting${encode.data ? ` (${encode.data.waiting.total})` : ""}, oldest first`}
      >
        <QueryState query={encode}>
          {(data) => (
            <>
              <EncodeJobsTable
                jobs={data.waiting.items}
                empty="No job waits."
                busy={busy}
                onCancel={cancel.mutate}
                onRequeue={requeue.mutate}
                onDismiss={onDismiss}
              />
              <Pagination
                page={waitingPage}
                total={data.waiting.total}
                pageSize={JOB_PAGE}
                onPage={setWaitingPage}
                label="Waiting pages"
              />
            </>
          )}
        </QueryState>
      </PageSection>
      <PageSection title="Finished">
        <QueryState query={encode}>
          {(data) => (
            <>
              <EncodeJobsTable
                jobs={data.finished.items}
                empty="No finished job."
                busy={busy}
                onCancel={cancel.mutate}
                onRequeue={requeue.mutate}
                onDismiss={onDismiss}
              />
              <Pagination
                page={finishedPage}
                total={data.finished.total}
                pageSize={JOB_PAGE}
                onPage={setFinishedPage}
                label="Finished pages"
              />
            </>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
