import { useEffect, useRef } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, mutations, type Source } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/channel"

// Matches the API's job reason length (backend/savetube/web/api.py _REASON_CHARS).
const ERROR_CHARS = 120

function shortError(error: string): string {
  const flat = error.split(/\s+/).join(" ").trim()
  return flat.length <= ERROR_CHARS ? flat : flat.slice(0, ERROR_CHARS - 3) + "..."
}

function WaitingSource({ source }: { source: Source }) {
  const queryClient = useQueryClient()
  const poll = useMutation({
    mutationFn: () => mutations.pollSource(source.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["sources"] }),
  })
  const label = source.title ?? source.url
  const error = source.last_error
  const short = error && shortError(error)

  return (
    <li className="flex items-start justify-between gap-3 p-3">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <Badge variant="outline">{source.kind}</Badge>
          <a
            className="min-w-0 truncate font-medium hover:underline"
            href={source.url}
            target="_blank"
            rel="noreferrer"
            title={source.url}
          >
            {label}
          </a>
        </div>
        {!source.enabled ? (
          <span className="text-xs text-muted-foreground">Disabled, not polled</span>
        ) : !error ? (
          <span className="text-xs text-muted-foreground">Waiting for first poll</span>
        ) : short === error ? (
          <span className="text-xs text-destructive">{error}</span>
        ) : (
          <details className="text-xs">
            <summary className="cursor-pointer text-destructive">{short}</summary>
            <pre className="mt-1 max-h-60 overflow-auto rounded bg-muted p-2 whitespace-pre-wrap break-all">
              {error}
            </pre>
          </details>
        )}
        {poll.error && <span className="text-xs text-destructive">{poll.error.message}</span>}
      </div>
      {source.enabled && (
        <Button variant="outline" size="sm" disabled={poll.isPending} onClick={() => poll.mutate()}>
          {poll.isPending ? "Polling" : "Poll now"}
        </Button>
      )}
    </li>
  )
}

// Sources without a channel yet: before their first poll, or whose first poll
// failed. The sidebar lists channels only, so these show nowhere else.
export function SourcesWaitingCard() {
  const queryClient = useQueryClient()
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const waiting = sources.data?.filter((s) => s.channel_id === null) ?? []

  // A source that leaves the set has usually gained a channel, which the
  // sidebar's channel list does not have yet.
  const previous = useRef<Set<number> | null>(null)
  const ids = waiting.map((s) => s.id).join(",")
  useEffect(() => {
    if (!sources.data) return
    const current = new Set(ids ? ids.split(",").map(Number) : [])
    if (previous.current && [...previous.current].some((id) => !current.has(id))) {
      queryClient.invalidateQueries({ queryKey: ["channels"] })
    }
    previous.current = current
  }, [ids, sources.data, queryClient])

  if (waiting.length === 0) return null

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Sources waiting</CardTitle>
        <CardDescription>
          {waiting.length} {waiting.length === 1 ? "source has" : "sources have"} no channel yet
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0">
        <ul className="flex flex-col divide-y">
          {waiting.map((s) => (
            <WaitingSource key={s.id} source={s} />
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
