import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router"

import { Clock } from "@/components/clock"
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
import { api, mutations, type AttentionItem, type AttentionKind } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"

const SHOWN = 10

const KIND_LABEL: Record<AttentionKind, string> = {
  archive_failed: "download failed",
  record_failed: "recording failed",
  replace_failed: "replace failed",
  encode_failed: "encode failed",
  held: "replace held",
  source_failing: "poll failing",
  cookie_signed_out: "cookie signed out",
}

function target(item: AttentionItem): string {
  if (item.video_id) return `/videos/${encodeURIComponent(item.video_id)}`
  if (item.channel_id) return `/channels/${encodeURIComponent(item.channel_id)}`
  return "/settings"
}

function AttentionLine({
  item,
  onDismiss,
  dismissing,
}: {
  item: AttentionItem
  onDismiss: () => void
  dismissing: boolean
}) {
  return (
    <li className="flex min-h-11 min-w-0 items-center gap-2 px-3 py-1 text-sm">
      <Badge variant={item.kind === "held" ? "outline" : "destructive"} className="shrink-0">
        {KIND_LABEL[item.kind]}
      </Badge>
      <Link className="min-w-0 truncate font-medium hover:underline" title={item.title} to={target(item)}>
        {item.title}
      </Link>
      {item.channel_name && (
        <span className="max-w-40 shrink-0 truncate text-xs text-muted-foreground" title={item.channel_name}>
          {item.channel_name}
        </span>
      )}
      {item.detail && (
        <span
          className="min-w-0 truncate text-xs text-muted-foreground"
          title={item.log_tail ?? item.detail}
        >
          {item.detail}
        </span>
      )}
      <span className="ml-auto shrink-0 text-xs text-muted-foreground">
        <Clock iso={item.at} />
      </span>
      {item.dismissable && (
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0"
          aria-label={`Dismiss ${KIND_LABEL[item.kind]}: ${item.title}`}
          disabled={dismissing}
          onClick={onDismiss}
        >
          Dismiss
        </Button>
      )}
    </li>
  )
}

// What fails or waits on the user outside the page that shows it, so one card
// replaces checking every page.
export function AttentionCard() {
  const queryClient = useQueryClient()
  const [all, setAll] = useState(false)
  const items = useQuery({
    queryKey: queryKeys.attention(),
    queryFn: api.attention,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const dismiss = useMutation({
    mutationFn: mutations.dismissAttention,
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.attention() }),
  })

  const rows = items.data ?? []
  if (rows.length === 0) return null
  const dismissable = rows.filter((i) => i.dismissable)
  const shown = all ? rows : rows.slice(0, SHOWN)

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Needs attention</CardTitle>
        <CardDescription>
          {rows.length} {rows.length === 1 ? "item" : "items"}
        </CardDescription>
        {dismissable.length > 0 && (
          <CardAction>
            <Button
              variant="outline"
              size="sm"
              disabled={dismiss.isPending}
              onClick={() => dismiss.mutate(dismissable.map(({ kind, key }) => ({ kind, key })))}
            >
              Dismiss all
            </Button>
          </CardAction>
        )}
        {dismiss.error && <p className="text-xs text-destructive">{dismiss.error.message}</p>}
      </CardHeader>
      <CardContent className="px-0">
        <ul className="flex flex-col divide-y">
          {shown.map((i) => (
            <AttentionLine
              key={`${i.kind}:${i.key}`}
              item={i}
              dismissing={dismiss.isPending}
              onDismiss={() => dismiss.mutate([{ kind: i.kind, key: i.key }])}
            />
          ))}
        </ul>
        {rows.length > SHOWN && (
          <Button variant="ghost" size="sm" className="m-2" onClick={() => setAll(!all)}>
            {all ? "Show fewer" : `Show ${rows.length - SHOWN} more`}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
