import { type FormEvent } from "react"
import { useMutation } from "@tanstack/react-query"

import { ChannelAvatar } from "@/components/channel-avatar"
import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { api, type ChannelScan } from "@/lib/api"
import { VIDEO_TABS } from "@/lib/channel"

export function ScanStep({
  url,
  onUrlChange,
  scan,
  onScanned,
}: {
  url: string
  onUrlChange: (url: string) => void
  scan: ChannelScan | null
  onScanned: (scan: ChannelScan) => void
}) {
  const run = useMutation({ mutationFn: api.channelScan, onSuccess: onScanned })

  function submit(e: FormEvent) {
    e.preventDefault()
    run.mutate(url.trim())
  }

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={submit} className="flex flex-col gap-2">
        <Label htmlFor="channel-url">Channel URL</Label>
        <div className="flex gap-2">
          <Input
            id="channel-url"
            required
            autoFocus
            placeholder="https://www.youtube.com/@handle"
            value={url}
            onChange={(e) => onUrlChange(e.target.value)}
          />
          <Button type="submit" disabled={!url.trim() || run.isPending}>
            {run.isPending ? "Scanning" : "Scan"}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          The scan lists the first page of the Videos, Shorts and Live tabs and the channel's
          playlists. It stores nothing.
        </p>
      </form>
      {run.isPending && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted-foreground">Listing the channel. This takes several seconds.</p>
          <Skeleton className="h-28 w-full" />
        </div>
      )}
      {run.isError && <ErrorBox error={run.error} />}
      {scan && !run.isPending && (
        <div className="flex flex-col gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2">
            <ChannelAvatar name={scan.name} url={scan.avatar_url} size="lg" />
            <span className="text-base font-medium">{scan.name}</span>
            {scan.handle && <span className="text-muted-foreground">{scan.handle}</span>}
            <span className="font-mono text-xs text-muted-foreground">{scan.channel_id}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {VIDEO_TABS.map(({ tab, label }) => (
              <Badge key={tab} variant="outline">
                {label}:{" "}
                {scan.tabs[tab] === null ? "no tab" : `${scan.tabs[tab]?.length ?? 0} on the first page`}
              </Badge>
            ))}
            <Badge variant="outline">Playlists: {scan.playlists.length}</Badge>
          </div>
        </div>
      )}
    </div>
  )
}
