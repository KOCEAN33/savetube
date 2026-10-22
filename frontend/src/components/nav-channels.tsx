import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { PlusIcon } from "lucide-react"
import { Link, useLocation } from "react-router"

import { AddChannelDialog } from "@/components/add-channel-dialog"
import { ChannelAvatar } from "@/components/channel-avatar"
import {
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarInput,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { api } from "@/lib/api"
import { formatCount } from "@/lib/format"
import { channelHref, channelIdOf } from "@/lib/nav"
import { queryKeys } from "@/lib/query-keys"

// The tracked channels by name, each with its stored-video count. The filter
// matches the name or the handle.
export function NavChannels() {
  const { pathname } = useLocation()
  const current = channelIdOf(pathname)
  const channels = useQuery({ queryKey: queryKeys.channels(), queryFn: api.channels })
  const [filter, setFilter] = useState("")
  const [adding, setAdding] = useState(false)
  const tracked = (channels.data ?? [])
    .filter((c) => c.tracked)
    .sort((a, b) => a.name.localeCompare(b.name))
  const term = filter.trim().toLowerCase()
  const shown = term
    ? tracked.filter(
        (c) => c.name.toLowerCase().includes(term) || c.handle?.toLowerCase().includes(term),
      )
    : tracked

  return (
    <SidebarGroup>
      <SidebarGroupLabel>Channels ({tracked.length})</SidebarGroupLabel>
      <SidebarGroupAction title="Add channel" onClick={() => setAdding(true)}>
        <PlusIcon />
        <span className="sr-only">Add channel</span>
      </SidebarGroupAction>
      <SidebarGroupContent className="flex flex-col gap-2">
        <SidebarInput
          type="search"
          aria-label="Filter channels"
          placeholder="Filter channels"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <SidebarMenu>
          {shown.map((c) => (
            <SidebarMenuItem key={c.id}>
              {/* The badge sits absolute over the button, so pad the name clear of it. */}
              <SidebarMenuButton
                asChild
                isActive={c.id === current}
                tooltip={c.name}
                className="pr-12"
              >
                <Link to={channelHref(c.id)}>
                  <ChannelAvatar name={c.name} url={c.avatar_url} size="sm" className="size-5" />
                  <span>{c.name}</span>
                </Link>
              </SidebarMenuButton>
              <SidebarMenuBadge>{formatCount(c.video_count)}</SidebarMenuBadge>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
        {channels.isError && (
          <p className="px-2 text-xs text-destructive">{channels.error.message}</p>
        )}
        {channels.isSuccess && shown.length === 0 && (
          <p className="px-2 text-xs text-muted-foreground">
            {tracked.length === 0 ? "No channels yet. Add one with +." : "No channel matches."}
          </p>
        )}
      </SidebarGroupContent>
      <AddChannelDialog open={adding} onOpenChange={setAdding} />
    </SidebarGroup>
  )
}
