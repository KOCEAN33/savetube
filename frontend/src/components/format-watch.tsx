import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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
import { formatBytes, formatDateTime, formatDuration, formatKbps } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

function resolution(row: FormatRow): string {
  if (row.kind === "audio") return "audio"
  if (row.height == null) return "-"
  return `${row.height}p${row.fps ? Math.round(row.fps) : ""}`
}

function signedSeconds(diff: number | null): string {
  if (diff == null) return "-"
  return `${diff > 0 ? "+" : ""}${diff.toFixed(1)} s`
}

// The stored file and the formats the VOD offers, for a live in its format
// watch. The pick of each kind carries a `recommended` badge, and any listed
// format can replace its stream by hand.
export function FormatWatchCard({ videoId }: { videoId: string }) {
  const queryClient = useQueryClient()
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

  if (watch.isError) return <ErrorBox error={watch.error} />
  const w = watch.data
  if (!w) return null
  const replaceActive = w.replace?.state === "queued" || w.replace?.state === "running"
  // The scheduler ends a watch within a minute of `ends_at`. The API refuses a pick after it.
  const open = w.ended_at == null
  const choose = (formatId: string) => {
    if (w.held) setConfirming(formatId)
    else pick.mutate({ format_id: formatId, confirm: false })
  }

  return (
    <Card id="format-watch">
      <CardHeader>
        <CardTitle>Formats</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <WatchStatus watch={w} />
        {pick.isError && <ErrorBox error={pick.error} />}
        <div className="overflow-x-auto rounded-lg border">
          <Table aria-label="Formats">
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
              {[...w.stored, ...w.formats].map((row) => (
                <TableRow key={`${row.stored ? "stored" : "vod"}-${row.kind}-${row.format_id}`}>
                  <TableCell>{row.stored ? `stored ${row.kind}` : row.kind}</TableCell>
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
                          disabled={!open || replaceActive || pick.isPending}
                          onClick={() => choose(row.format_id)}
                        >
                          Use
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
      <Dialog open={confirming != null} onOpenChange={(o) => !o && setConfirming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Replace a held video?</DialogTitle>
            <DialogDescription>
              The VOD and the stored file differ by {signedSeconds(w.duration_diff)}, so the
              VOD may miss a part the stored file has. The stored file stays aside until the
              watch ends. Automatic replaces stop for this video.
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

function WatchStatus({ watch: w }: { watch: FormatWatch }) {
  const ended = w.ended_at != null
  return (
    <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Watch</dt>
        <dd>
          {ended ? `ended ${formatDateTime(w.ended_at)}` : `until ${formatDateTime(w.ends_at)}`}
        </dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Last probe</dt>
        <dd>{formatDateTime(w.checked_at)}</dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Replaces</dt>
        <dd>
          {w.held && (
            <Badge variant="destructive">held: {signedSeconds(w.duration_diff)}</Badge>
          )}{" "}
          {w.manual ? "manual pick only" : "automatic"}
        </dd>
      </div>
      <div className="flex flex-col gap-0.5">
        <dt className="text-xs text-muted-foreground">Last replace</dt>
        <dd>
          {w.replace
            ? `${w.replace.format_id ?? "-"}: ${w.replace.state}${w.replace.reason ? `, ${w.replace.reason}` : ""}`
            : "-"}
          {w.original_kept && <span className="text-muted-foreground"> (original kept)</span>}
        </dd>
      </div>
      {w.probe_error && (
        <p className="col-span-full text-xs text-destructive">{w.probe_error}</p>
      )}
    </dl>
  )
}
