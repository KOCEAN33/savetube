import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PlusIcon } from "lucide-react"
import { Link } from "react-router"

import { AddChannelDialog } from "@/components/add-channel-dialog"
import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type Channel, type Source } from "@/lib/api"
import { VIDEO_TABS } from "@/lib/channel"
import { formatBytes, formatCount } from "@/lib/format"

function tabOf(s: Source): string {
  return s.url.slice(s.url.lastIndexOf("/") + 1)
}

function ChannelRow({ channel, sources }: { channel: Channel; sources: Source[] }) {
  const top = sources.filter((s) => s.parent_id === null)
  const tabs = VIDEO_TABS.filter(({ tab }) =>
    top.some((s) => s.kind === "tab" && s.enabled && tabOf(s) === tab),
  )
  const tracksAll = top.some((s) => s.kind === "playlists" && s.enabled)
  const playlists = sources.filter((s) => s.kind === "playlist" && s.enabled).length
  const paused = top.length > 0 && top.every((s) => !s.archiving)
  const errors = sources.filter((s) => s.enabled && s.last_error).length

  return (
    <TableRow>
      <TableCell>
        <Link
          className="font-medium hover:underline"
          to={`/channels/${encodeURIComponent(channel.id)}`}
        >
          {channel.name}
        </Link>
        <div className="text-xs text-muted-foreground">{channel.handle ?? channel.id}</div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {tabs.map(({ tab, label }) => (
            <Badge key={tab} variant="outline">
              {label}
            </Badge>
          ))}
          {(tracksAll || playlists > 0) && (
            <Badge variant="outline">
              {tracksAll ? "all playlists" : "playlists"} ({playlists})
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {channel.watch_lives && <Badge>recording lives</Badge>}
          {paused && <Badge variant="secondary">paused</Badge>}
          {errors > 0 && <Badge variant="destructive">{errors} failing</Badge>}
        </div>
      </TableCell>
      <TableCell className="text-right">{formatCount(channel.video_count)}</TableCell>
      <TableCell className="text-right">{formatBytes(channel.bytes)}</TableCell>
    </TableRow>
  )
}

export function ChannelsPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources, refetchInterval: 15_000 })
  const [adding, setAdding] = useState(false)
  const byChannel = new Map<string, Source[]>()
  for (const s of sources.data ?? []) {
    if (s.channel_id) byChannel.set(s.channel_id, [...(byChannel.get(s.channel_id) ?? []), s])
  }
  const orphans = sources.data?.filter((s) => s.channel_id === null && s.parent_id === null) ?? []

  return (
    <Page>
      <PageSection
        title="Channels"
        actions={
          <Button size="sm" onClick={() => setAdding(true)}>
            <PlusIcon /> Add channel
          </Button>
        }
      >
        <QueryState query={channels}>
          {(rows) => {
            // A channel known only as another playlist's uploader has no source.
            const added = rows.filter((c) => byChannel.has(c.id))
            return (
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>Channel</TableHead>
                    <TableHead>Archiving</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Videos</TableHead>
                    <TableHead className="text-right">Size</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {added.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={5} className="text-center text-muted-foreground">
                        {sources.isPending ? "Loading." : "No channels yet. Add one to start."}
                      </TableCell>
                    </TableRow>
                  )}
                  {added.map((c) => (
                    <ChannelRow key={c.id} channel={c} sources={byChannel.get(c.id) ?? []} />
                  ))}
                </TableBody>
              </Table>
            )
          }}
        </QueryState>
      </PageSection>
      {orphans.length > 0 && (
        <PageSection title="Sources without a channel yet">
          <ul className="flex flex-col divide-y text-sm">
            {orphans.map((s) => (
              <li key={s.id} className="flex flex-col gap-0.5 p-3">
                <a className="truncate hover:underline" href={s.url} target="_blank" rel="noreferrer">
                  {s.title ?? s.url}
                </a>
                <span className="text-xs text-muted-foreground">
                  {s.last_error ?? "Its first poll finds the channel."}
                </span>
              </li>
            ))}
          </ul>
        </PageSection>
      )}
      <AddChannelDialog open={adding} onOpenChange={setAdding} />
    </Page>
  )
}
