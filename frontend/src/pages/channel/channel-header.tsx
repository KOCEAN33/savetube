import { ExternalLinkIcon } from "lucide-react"

import { ChannelAvatar } from "@/components/channel-avatar"
import { Badge } from "@/components/ui/badge"
import { type Channel, type Source } from "@/lib/api"
import { channelBase } from "@/lib/channel"
import { formatBytes, formatCount, formatDateTime } from "@/lib/format"

export function ChannelHeader({ channel, sources }: { channel: Channel; sources: Source[] }) {
  const top = sources.filter((s) => s.parent_id === null)
  const paused = top.length > 0 && top.every((s) => !s.archiving)
  const failing = sources.filter((s) => s.enabled && s.last_error).length
  const lastPoll = sources
    .map((s) => s.last_polled_at)
    .filter((t) => t !== null)
    .sort()
    .at(-1)
  const stats = [
    ["Stored", formatCount(channel.video_count)],
    ["Size", formatBytes(channel.bytes)],
    ["Sources", formatCount(sources.length)],
    ["Last poll", lastPoll ? formatDateTime(lastPoll) : "waiting"],
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
      <div className="flex min-w-0 items-center gap-3">
        <ChannelAvatar name={channel.name} url={channel.avatar_url} className="size-12" />
        <div className="flex min-w-0 flex-col">
          <h2 className="truncate text-lg font-semibold" title={channel.name}>
            {channel.name}
          </h2>
          <a
            className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:underline"
            href={channelBase(channel.id, sources)}
            target="_blank"
            rel="noreferrer"
          >
            {channel.handle ?? channel.id}
            <ExternalLinkIcon className="size-3" />
          </a>
        </div>
      </div>
      <dl className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
        {stats.map(([label, value]) => (
          <div key={label} className="flex items-baseline gap-1.5">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap gap-1">
        {channel.watch_lives && <Badge>recording lives</Badge>}
        {paused && <Badge variant="secondary">paused</Badge>}
        {failing > 0 && <Badge variant="destructive">{failing} failing</Badge>}
      </div>
    </div>
  )
}
