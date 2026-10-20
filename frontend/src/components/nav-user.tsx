import { useQuery } from "@tanstack/react-query"
import { EllipsisVerticalIcon, LogOutIcon } from "lucide-react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { api, type Me } from "@/lib/api"

function initials(me: Me | undefined): string {
  const source = me?.name || me?.email || me?.sub || "?"
  return source
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("")
}

function UserSummary({ me }: { me: Me | undefined }) {
  return (
    <>
      <Avatar className="h-8 w-8 rounded-lg">
        <AvatarFallback className="rounded-lg">{initials(me)}</AvatarFallback>
      </Avatar>
      <div className="grid flex-1 text-left text-sm leading-tight">
        <span className="truncate font-medium">
          {me ? (me.name ?? me.sub) : "Loading"}
        </span>
        <span className="truncate text-xs text-muted-foreground">
          {me?.email ?? ""}
        </span>
      </div>
    </>
  )
}

export function NavUser() {
  const { isMobile } = useSidebar()
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: api.me })

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
            >
              <UserSummary me={me} />
              <EllipsisVerticalIcon className="ml-auto size-4" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56 rounded-lg"
            side={isMobile ? "bottom" : "right"}
            align="end"
            sideOffset={4}
          >
            <DropdownMenuLabel className="p-0 font-normal">
              <div className="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
                <UserSummary me={me} />
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <form method="post" action="/auth/logout">
              <DropdownMenuItem asChild>
                <button type="submit" className="w-full">
                  <LogOutIcon />
                  Log out
                </button>
              </DropdownMenuItem>
            </form>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
