import { useQuery } from "@tanstack/react-query"
import { useLocation } from "react-router"

import { DownloadPauseToggle } from "@/components/download-pause"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { api } from "@/lib/api"
import { channelIdOf, navTitle } from "@/lib/nav"

function usePageTitle(): string {
  const { pathname } = useLocation()
  const channelId = channelIdOf(pathname)
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  if (channelId === null) return navTitle(pathname)
  return channels.data?.find((c) => c.id === channelId)?.name ?? "Channel"
}

export function SiteHeader() {
  const title = usePageTitle()

  return (
    <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-(--header-height)">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mx-2 data-[orientation=vertical]:h-4"
        />
        <h1 className="truncate text-base font-medium">{title}</h1>
        <div className="ml-auto">
          <DownloadPauseToggle />
        </div>
      </div>
    </header>
  )
}
