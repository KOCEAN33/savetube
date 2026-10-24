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
import { formatBytes, formatClock, formatDuration, formatKbps } from "@/lib/format"
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

function signedSeconds(diff: number | null): string {
  if (diff == null) return "-"
  return `${diff > 0 ? "+" : ""}${diff.toFixed(1)} s`
}

// The one-line state of the collapsed card: stored codecs, the upgrade the
// last probe recommends, and when it ran.
function summary(w: FormatWatch): string {
  const stored = (["video", "audio"] as const)
    .map((k) => w.stored.find((r) => r.kind === k)?.codec)
    .filter(Boolean)
    .join(" + ")
  const upgrades = w.formats.filter((r) => r.recommended)
  const upgrade = upgrades.length
    ? `upgrade: ${upgrades.map((r) => `${r.codec} (${r.format_id})`).join(", ")}`
    : w.formats.length
      ? "no upgrade"
      : "no formats listed"
  const probed = w.checked_at ? `probed ${formatClock(w.checked_at)}` : "never probed"
  return [`stored ${stored || "-"}`, upgrade, probed].join(" / ")
}

// The stored file and the formats YouTube offers for it. A live's automatic
// watch probes for 7 days, and "Probe now" lists the formats of any complete
// video. The pick of each kind carries a `recommended` badge, and any listed
// format can replace its stream by hand.
export function FormatWatchCard({ videoId }: { videoId: string }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)
  const watch = useQuery({
    queryKey: queryKeys.formatWatch(videoId),
    queryFn: () => api.formatWatch(videoId),
    refetchInterval: (query) => {
      const state = query.state.data?.replace?.state
      return refetchMs(state === "queued" || state === "running")
    },
  })
  const pick = useMutation({
    mutationFn: (body: { format_id: string; confirm: boolean }) =>
      mutations.pickFormat(videoId, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.formatWatch(videoId), updated)
      setConfirming(null)
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

  const replaceActive = w.replace?.state === "queued" || w.replace?.state === "running"
  const choose = (formatId: string) => {
    if (w.held) setConfirming(formatId)
    else pick.mutate({ format_id: formatId, confirm: false })
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
                  disabled={replaceActive || pick.isPending}
                  onUse={choose}
                />
              ))}
            </>
          )}
        </CardContent>
      )}
      <Dialog open={confirming != null} onOpenChange={(o) => !o && setConfirming(null)}>
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
              onClick={() => confirming && pick.mutate({ format_id: confirming, confirm: true })}
            >
              Replace with {confirming}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

// One kind's rows: the stored stream and the recommended format first, the
// rest behind a toggle.
function FormatSection({
  kind,
  watch: w,
  disabled,
  onUse,
}: {
  kind: Kind
  watch: FormatWatch
  disabled: boolean
  onUse: (formatId: string) => void
}) {
  const [all, setAll] = useState(false)
  const stored = w.stored.filter((r) => r.kind === kind)
  const offered = w.formats.filter((r) => r.kind === kind)
  const first = [...stored, ...offered.filter((r) => r.recommended)]
  const rest = offered.filter((r) => !r.recommended)
  const rows = all ? [...first, ...rest] : first
  const label = `${KIND_LABEL[kind]} formats`

  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">{KIND_LABEL[kind]}</h3>
      <div className="overflow-x-auto rounded-lg border">
        <Table aria-label={label}>
          <TableHeader>
            <TableRow>
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
              <TableRow key={`${row.stored ? "stored" : "vod"}-${row.format_id}`}>
                <TableCell>{row.stored ? "stored" : "YouTube"}</TableCell>
                <TableCell className="font-mono">{row.format_id}</TableCell>
                <TableCell>{row.codec}</TableCell>
                <TableCell>{resolution(row)}</TableCell>
                <TableCell>{formatKbps(row.bitrate)}</TableCell>
                <TableCell>{row.stored ? "-" : formatBytes(row.size)}</TableCell>
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
                    {!row.stored && (
                      <Button
                        size="sm"
                        variant="outline"
                        aria-label={`Use format ${row.format_id}`}
                        disabled={disabled}
                        onClick={() => onUse(row.format_id)}
                      >
                        Use
                      </Button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={9} className="text-muted-foreground">
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
