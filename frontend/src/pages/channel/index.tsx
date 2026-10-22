import { useState } from "react"
import { useQueries, useQuery } from "@tanstack/react-query"
import { ArrowDownIcon, ArrowUpIcon, SearchIcon } from "lucide-react"
import { useParams, useSearchParams } from "react-router"

import { PlaylistFolds, StoredVideos } from "@/components/channel-videos"
import { ErrorBox, Page } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useDebounced } from "@/hooks/use-debounced"
import { api, type Channel, type Source, type VideoOrder, type VideoType } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatCount } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { ChannelHeader } from "@/pages/channel/channel-header"
import { DownloadsTab } from "@/pages/channel/downloads-tab"

const TABS = ["videos", "shorts", "live", "members", "playlists", "downloads"] as const
type ChannelTab = (typeof TABS)[number]
const TAB_LABEL: Record<ChannelTab, string> = {
  videos: "Videos",
  shorts: "Shorts",
  live: "Live",
  members: "Members",
  playlists: "Playlists",
  downloads: "Downloads",
}
const TAB_TYPE: Partial<Record<ChannelTab, VideoType>> = {
  videos: "videos",
  shorts: "shorts",
  live: "lives",
  members: "members",
}
const isTab = (v: string | null): v is ChannelTab => TABS.includes(v as ChannelTab)

// Complete videos per type by the channel list rule, for the tab counts.
function useTypeTotals(channelId: string): Partial<Record<VideoType, number>> {
  const types = Object.values(TAB_TYPE)
  const results = useQueries({
    queries: types.map((type) => ({
      queryKey: queryKeys.typeTotal({ listedBy: channelId, type, state: "complete", limit: 1 }),
      queryFn: () => api.videos({ listed_by: channelId, type, state: "complete", limit: 1 }),
    })),
  })
  return Object.fromEntries(types.map((t, i) => [t, results[i].data?.total]))
}

function ChannelView({ channel, sources }: { channel: Channel; sources: Source[] }) {
  const [params, setParams] = useSearchParams()
  const param = params.get("tab")
  const tab: ChannelTab = isTab(param) ? param : "videos"
  const [search, setSearch] = useState<Partial<Record<ChannelTab, string>>>({})
  const [order, setOrder] = useState<VideoOrder>("newest")
  const q = search[tab] ?? ""
  const term = useDebounced(q.trim(), 300)
  const totals = useTypeTotals(channel.id)
  const playlists = sources.filter((s) => s.kind === "playlist")
  const type = TAB_TYPE[tab]
  // Members shows only while the channel has a members-only video, or while the URL picks it.
  const tabs = TABS.filter((t) => t !== "members" || (totals.members ?? 0) > 0 || tab === t)
  const count = (t: ChannelTab) => {
    const ty = TAB_TYPE[t]
    if (ty) return totals[ty]
    if (t === "playlists") return playlists.length
    return undefined
  }

  function selectTab(next: ChannelTab) {
    const updated = new URLSearchParams(params)
    if (next === "videos") updated.delete("tab")
    else updated.set("tab", next)
    setParams(updated)
  }

  return (
    <div className="flex flex-col gap-5 px-4 py-4 md:py-6 lg:px-6">
      <ChannelHeader channel={channel} sources={sources} />
      {/* Underlined tabs on a full-width rule, like YouTube's channel tabs. The
          ToggleGroup keeps its radiogroup role, which the smoke test targets. */}
      <div className="flex flex-wrap items-end gap-x-3 gap-y-2 border-b">
        <ToggleGroup
          type="single"
          size="lg"
          spacing={1}
          aria-label="Channel section"
          value={tab}
          onValueChange={(v) => isTab(v) && selectTab(v)}
          className="flex-wrap"
        >
          {tabs.map((t) => (
            <ToggleGroupItem
              key={t}
              value={t}
              className="-mb-px h-11 gap-1.5 rounded-none border-b-2 border-transparent px-4 text-base text-muted-foreground hover:bg-transparent hover:text-foreground data-[state=on]:border-foreground data-[state=on]:bg-transparent data-[state=on]:text-foreground"
            >
              {TAB_LABEL[t]}
              {count(t) !== undefined && (
                <span className="text-sm tabular-nums opacity-60">{formatCount(count(t) ?? 0)}</span>
              )}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {tab !== "downloads" && (
          <div className="ml-auto flex flex-wrap items-center gap-2 pb-2">
            <div className="relative">
              <SearchIcon className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
              <Input
                type="search"
                aria-label={`Search ${TAB_LABEL[tab]}`}
                placeholder={tab === "playlists" ? "Filter playlists" : `Search ${TAB_LABEL[tab]}`}
                className="w-56 pl-8"
                value={q}
                onChange={(e) => setSearch({ ...search, [tab]: e.target.value })}
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOrder(order === "newest" ? "oldest" : "newest")}
            >
              {order === "newest" ? <ArrowDownIcon /> : <ArrowUpIcon />}
              {order === "newest" ? "Newest first" : "Oldest first"}
            </Button>
          </div>
        )}
      </div>
      {tab === "downloads" ? (
        <DownloadsTab channel={channel} sources={sources} />
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {type ? (
            <StoredVideos
              scope={{ listedBy: channel.id, type }}
              q={term}
              order={order}
              tiles={type === "shorts"}
            />
          ) : (
            <PlaylistFolds playlists={playlists} q={term} order={order} />
          )}
        </div>
      )}
    </div>
  )
}

export function ChannelPage() {
  const { id = "" } = useParams()
  const channels = useQuery({ queryKey: queryKeys.channels(), queryFn: api.channels })
  const sources = useQuery({
    queryKey: queryKeys.sources(),
    queryFn: api.sources,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const channel = channels.data?.find((c) => c.id === id)
  const own = sources.data?.filter((s) => s.channel_id === id) ?? []

  if (channels.isPending || sources.isPending)
    return (
      <Page>
        <Skeleton className="h-64 w-full" />
      </Page>
    )
  if (channels.isError) return <Page><ErrorBox error={channels.error} /></Page>
  if (sources.isError) return <Page><ErrorBox error={sources.error} /></Page>
  if (!channel)
    return (
      <Page>
        <p className="text-sm text-muted-foreground">No channel {id}.</p>
      </Page>
    )

  // Keyed so the search boxes and the sort start fresh on another channel.
  return <ChannelView key={channel.id} channel={channel} sources={own} />
}
