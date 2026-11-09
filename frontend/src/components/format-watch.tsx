import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronRightIcon } from "lucide-react"

import { Clock } from "@/components/clock"
import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, mutations, type FormatRow, type FormatWatch } from "@/lib/api"
import { formatBytes, formatClock, formatDuration, formatKbps, signedSeconds } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"
import { cn } from "@/lib/utils"

type Kind = FormatRow["kind"]

const KIND_LABEL: Record<Kind, string> = { video: "Video", audio: "Audio" }

function resolution(row: FormatRow): string {
  if (row.kind === "audio") return "audio"
  if (row.height == null) return "-"
  return `${row.height}p${row.fps ? Math.round(row.fps) : ""}`
}

// The whole file before and after a re-encode, such as "file 11.1 GiB → 4.8 GiB (-56%)".
function encodeSaving(sourceSize: number, resultSize: number): string {
  const saved = Math.round((1 - resultSize / sourceSize) * 100)
  return `file ${formatBytes(sourceSize)} → ${formatBytes(resultSize)} (-${saved}%)`
}

const KIND_ORDER: Kind[] = ["video", "audio"]

// "AV1 (399) + Opus (251)": video first, an id the probe no longer lists as is.
function formatList(ids: string[], formats: FormatRow[]): string {
  return ids
    .map(
      (id) =>
        formats.find((r) => r.format_id === id) ?? { format_id: id, codec: id, kind: "video" as Kind },
    )
    .sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind))
    .map((r) => (r.codec === r.format_id ? r.format_id : `${r.codec} (${r.format_id})`))
    .join(" + ")
}

// The one-line state of the collapsed card: stored codecs, then the replace
// queued or running, else the upgrade the last probe recommends and whether
// it needs a pick, and when the probe ran.
function summary(w: FormatWatch): string {
  const storedRows = [...w.stored, ...w.formats.filter((r) => r.is_stored)]
  const stored = KIND_ORDER.map((k) => storedRows.find((r) => r.kind === k)?.codec)
    .filter(Boolean)
    .join(" + ")
  const probed = w.checked_at ? `probed ${formatClock(w.checked_at)}` : "never probed"
  const r = w.replace
  if (r && (r.state === "queued" || r.state === "running")) {
    // A replace downloads only the streams that change: the other keeps its stored codec.
    const ids = r.format_id?.split("+") ?? []
    const target = KIND_ORDER.map((k) => {
      const id = ids.find((i) => w.formats.find((row) => row.format_id === i)?.kind === k)
      return id ? formatList([id], w.formats) : storedRows.find((row) => row.kind === k)?.codec
    })
      .filter(Boolean)
      .join(" + ")
    // An automatic replace starts only once no download waits (archive.md, Download limits).
    const when =
      r.state === "running"
        ? "replacing now"
        : r.manual
          ? "replace queued"
          : "replace queued, starts once no download waits"
    return [`stored ${stored || "-"} → ${target}, ${when}`, probed].join(" / ")
  }
  const upgrades = w.formats.filter((row) => row.recommended && !row.is_stored)
  let upgrade = w.formats.length ? "no upgrade" : "no formats listed"
  if (upgrades.length) {
    upgrade = `upgrade: ${formatList(
      upgrades.map((row) => row.format_id),
      w.formats,
    )}`
    const automatic = w.started_at != null && w.ended_at == null && !w.manual
    if (w.held) upgrade += `, held (${signedSeconds(w.duration_diff)}), pick to replace`
    else if (!automatic) upgrade += ", pick to replace"
  }
  return [`stored ${stored || "-"}`, upgrade, probed].join(" / ")
}

// The radio value of the stored row, or of the YouTube row that is the
// stored stream: keep the stored stream of that kind.
const STORED = "stored"

function value(row: FormatRow): string {
  return row.stored || row.is_stored ? STORED : row.format_id
}

// The selected row of a kind: the user's choice while the table still lists
// it, else the stored row. A preselected upgrade reads as the stored stream.
function selection(w: FormatWatch, kind: Kind, chosen: string | undefined): string {
  const offered = w.formats.filter((r) => r.kind === kind)
  return chosen && offered.some((r) => r.format_id === chosen) ? chosen : STORED
}

// The stored file and the formats YouTube offers for it, best first. A live's
// automatic watch probes for 7 days, and "Probe now" lists the formats of any
// complete video. The pick of each kind carries a `recommended` badge. One
// video and one audio format can replace the stored streams by hand.
export function FormatWatchCard({ videoId }: { videoId: string }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [chosen, setChosen] = useState<Partial<Record<Kind, string>>>({})
  const watch = useQuery({
    queryKey: queryKeys.formatWatch(videoId),
    queryFn: () => api.formatWatch(videoId),
    refetchInterval: (query) => {
      const state = query.state.data?.replace?.state
      return refetchMs(state === "queued" || state === "running")
    },
  })
  const pick = useMutation({
    mutationFn: (body: { format_ids: string[]; confirm: boolean }) =>
      mutations.pickFormat(videoId, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.formatWatch(videoId), updated)
      setConfirming(false)
      setChosen({})
    },
  })
  const probe = useMutation({
    mutationFn: () => mutations.probeFormats(videoId),
    onSuccess: (updated) => queryClient.setQueryData(queryKeys.formatWatch(videoId), updated),
  })

  if (watch.isError) return <ErrorBox error={watch.error} />
  if (watch.isPending) return null
  const w = watch.data
  const probeButton = (
    <Button
      size="sm"
      variant="outline"
      disabled={probe.isPending}
      onClick={() => probe.mutate()}
    >
      {probe.isPending ? "Probing..." : "Probe now"}
    </Button>
  )

  if (!w) {
    return (
      <Card id="format-watch">
        <CardHeader>
          <CardTitle>Formats</CardTitle>
          <CardDescription>
            No formats listed yet. A probe lists what YouTube offers for this video now.
          </CardDescription>
          <CardAction>{probeButton}</CardAction>
        </CardHeader>
        {probe.isError && (
          <CardContent>
            <ErrorBox error={probe.error} />
          </CardContent>
        )}
      </Card>
    )
  }

  // A queued automatic replace gives way to a pick. A running replace or a
  // queued pick does not.
  const supersedable = w.replace?.state === "queued" && !w.replace.manual
  const blocked =
    w.replace?.state === "running" || (w.replace?.state === "queued" && w.replace.manual)
  const selected: Record<Kind, string> = {
    video: selection(w, "video", chosen.video),
    audio: selection(w, "audio", chosen.audio),
  }
  const picked = (["video", "audio"] as const)
    .map((k) => selected[k])
    .filter((id) => id !== STORED)
  const replaceLabel = picked.length ? `Replace with ${picked.join(" + ")}` : "Replace"
  const replace = () => {
    if (w.held) setConfirming(true)
    else pick.mutate({ format_ids: picked, confirm: false })
  }

  return (
    <Card id="format-watch">
      <CardHeader>
        <CardTitle>
          <button
            type="button"
            className="flex items-center gap-1 hover:underline"
            aria-expanded={open}
            aria-controls="format-watch-details"
            onClick={() => setOpen(!open)}
          >
            <ChevronRightIcon
              className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")}
            />
            Formats
          </button>
        </CardTitle>
        <CardDescription>{summary(w)}</CardDescription>
        <CardAction>{probeButton}</CardAction>
      </CardHeader>
      {(open || probe.isError) && (
        <CardContent id="format-watch-details" className="flex flex-col gap-4">
          {probe.isError && <ErrorBox error={probe.error} />}
          {open && (
            <>
              <WatchStatus watch={w} />
              {pick.isError && <ErrorBox error={pick.error} />}
              {(["video", "audio"] as const).map((kind) => (
                <FormatSection
                  key={kind}
                  kind={kind}
                  watch={w}
                  selected={selected[kind]}
                  disabled={blocked || pick.isPending}
                  onSelect={(id) => setChosen({ ...chosen, [kind]: id })}
                />
              ))}
              <div className="flex flex-col gap-2">
                {supersedable && (
                  <p className="text-sm text-muted-foreground">
                    The queued automatic replace ({w.replace?.format_id ?? "-"}) is cancelled
                    and replaced by this pick.
                  </p>
                )}
                <Button
                  className="w-fit"
                  disabled={blocked || pick.isPending || picked.length === 0}
                  onClick={replace}
                >
                  {replaceLabel}
                </Button>
              </div>
            </>
          )}
        </CardContent>
      )}
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Replace a held video?</DialogTitle>
            <DialogDescription>
              The VOD and the stored file differ by {signedSeconds(w.duration_diff)}, so the
              VOD may miss a part the stored file has. The stored file stays aside until the
              watch ends, or for 7 days without an open watch. Automatic replaces stop for
              this video.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={pick.isPending}
              onClick={() => pick.mutate({ format_ids: picked, confirm: true })}
            >
              {replaceLabel}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

// The offered formats a collapsed table shows, best first.
const TOP_FORMATS = 3

// One kind's rows: the stored stream first, then the offered formats in the
// API's best-first order. A stored YouTube stream has no stored row: its
// YouTube row stands for it and moves to the top. The tail behind a toggle
// starts after the top formats and the selected one. One radio per row picks
// the format, the stored row keeps the stored stream.
function FormatSection({
  kind,
  watch: w,
  selected,
  disabled,
  onSelect,
}: {
  kind: Kind
  watch: FormatWatch
  selected: string
  disabled: boolean
  onSelect: (formatId: string) => void
}) {
  const [all, setAll] = useState(false)
  const ofKind = w.formats.filter((r) => r.kind === kind)
  const stored = [
    ...w.stored.filter((r) => r.kind === kind),
    ...ofKind.filter((r) => r.is_stored),
  ]
  const offered = ofKind.filter((r) => !r.is_stored)
  const last = offered.findIndex((r) => r.format_id === selected)
  const shown = Math.max(TOP_FORMATS, last + 1)
  const rest = offered.slice(shown)
  const rows = [...stored, ...(all ? offered : offered.slice(0, shown))]
  const label = `${KIND_LABEL[kind]} formats`

  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">{KIND_LABEL[kind]}</h3>
      <div className="overflow-x-auto rounded-lg border">
        <Table aria-label={label}>
          <TableHeader>
            <TableRow>
              <TableHead>
                <span className="sr-only">Pick</span>
              </TableHead>
              <TableHead>Stream</TableHead>
              <TableHead>ID</TableHead>
              <TableHead>Codec</TableHead>
              <TableHead>Resolution</TableHead>
              <TableHead>Bitrate</TableHead>
              <TableHead>Size</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Difference</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow
                key={`${row.stored ? "stored" : "vod"}-${row.format_id}`}
                className={cn(value(row) === STORED && "bg-muted/60")}
              >
                <TableCell>
                  <input
                    type="radio"
                    name={`format-${kind}`}
                    className="size-4 accent-primary"
                    aria-label={
                      value(row) === STORED
                        ? `Keep stored ${kind}`
                        : `Select format ${row.format_id}`
                    }
                    checked={selected === value(row)}
                    disabled={disabled}
                    onChange={() => onSelect(value(row))}
                  />
                </TableCell>
                <TableCell>
                  {row.stored
                    ? row.encoded
                      ? "encoded"
                      : row.recorded
                        ? "recorded"
                        : "stored"
                    : row.is_stored
                      ? "YouTube, stored"
                      : "YouTube"}
                </TableCell>
                <TableCell className="font-mono">{row.format_id}</TableCell>
                <TableCell>{row.codec}</TableCell>
                <TableCell>{resolution(row)}</TableCell>
                <TableCell>{formatKbps(row.bitrate)}</TableCell>
                <TableCell>
                  {row.size_estimated ? "≈ " : ""}
                  {formatBytes(row.size)}
                </TableCell>
                <TableCell>
                  {formatDuration(row.stored ? w.stored_duration : w.vod_duration)}
                </TableCell>
                <TableCell>{row.stored ? "-" : signedSeconds(w.duration_diff)}</TableCell>
                <TableCell>
                  <div className="flex items-center justify-end gap-2">
                    {row.recommended && (
                      <Badge title={row.reason ?? undefined}>recommended</Badge>
                    )}
                    {row.recommended && row.reason && (
                      <span className="text-xs text-muted-foreground">{row.reason}</span>
                    )}
                    {row.encoded && w.encode?.result_size != null && (
                      <span className="text-xs text-muted-foreground">
                        {encodeSaving(w.encode.source_size, w.encode.result_size)}
                      </span>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={10} className="text-muted-foreground">
                  No {kind} stream listed.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      {rest.length > 0 && (
        <Button
          size="sm"
          variant="ghost"
          className="w-fit"
          aria-expanded={all}
          onClick={() => setAll(!all)}
        >
          {all ? "Show fewer" : `Show ${rest.length} more`}
        </Button>
      )}
    </section>
  )
}

function WatchStatus({ watch: w }: { watch: FormatWatch }) {
  const automatic = w.started_at != null
  let state = "no automatic watch"
  if (w.ended_at != null) state = `ended ${formatClock(w.ended_at)}`
  else if (automatic) state = `until ${formatClock(w.ends_at)}`
  return (
    <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Watch</dt>
        <dd>{state}</dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Last probe</dt>
        <dd>
          <Clock iso={w.checked_at} />
        </dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Replaces</dt>
        <dd>
          {w.held && (
            <Badge variant="destructive">held: {signedSeconds(w.duration_diff)}</Badge>
          )}{" "}
          {w.manual || !automatic || w.ended_at != null ? "manual pick only" : "automatic"}
        </dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Last replace</dt>
        <dd>
          {w.replace
            ? `${w.replace.format_id ?? "-"}: ${w.replace.state}${w.replace.reason ? `, ${w.replace.reason}` : ""}`
            : "-"}
          {w.original_kept && (
            <span className="text-muted-foreground">
              {" "}
              (original kept
              {w.original_until ? ` until ${formatClock(w.original_until)}` : ""})
            </span>
          )}
        </dd>
      </div>
      {w.probe_error && (
        <p className="col-span-full text-xs text-destructive">{w.probe_error}</p>
      )}
    </dl>
  )
}
