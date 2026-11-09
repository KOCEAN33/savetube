import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { Clock } from "@/components/clock"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, type HeldReplace } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { signedSeconds } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"

function HeldLine({ held }: { held: HeldReplace }) {
  const upgrade = held.upgrade.map((u) => `${u.codec} (${u.format_id})`).join(" + ")
  return (
    <li className="flex min-w-0 items-baseline gap-2 px-3 py-2 text-sm">
      <Badge variant="destructive">{signedSeconds(held.duration_diff)}</Badge>
      <Link
        className="min-w-0 truncate font-medium hover:underline"
        title={held.title}
        to={`/videos/${encodeURIComponent(held.video_id)}`}
      >
        {held.title}
      </Link>
      <span className="min-w-0 truncate text-xs text-muted-foreground" title={held.channel_name}>
        {held.channel_name}
      </span>
      <span className="shrink-0 text-xs">{upgrade}</span>
      <span className="ml-auto shrink-0 text-xs text-muted-foreground">
        <Clock iso={held.checked_at} />
      </span>
    </li>
  )
}

// Replaces the VOD's duration holds: only a pick on the video page starts
// them, so without this card they wait unseen.
export function HeldReplacesCard() {
  const held = useQuery({
    queryKey: queryKeys.heldReplaces(),
    queryFn: api.heldReplaces,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const rows = held.data ?? []
  if (rows.length === 0) return null

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Held replaces</CardTitle>
        <CardDescription>
          {rows.length} {rows.length === 1 ? "upgrade waits" : "upgrades wait"} for a pick: the VOD
          and the stored file differ by more than 2 s
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0">
        <ul className="flex flex-col divide-y">
          {rows.map((h) => (
            <HeldLine key={h.video_id} held={h} />
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
