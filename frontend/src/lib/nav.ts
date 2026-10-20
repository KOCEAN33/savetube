import {
  CircleDotIcon,
  FilmIcon,
  LayoutDashboardIcon,
  ListFilterIcon,
  RssIcon,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  title: string
  url: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { title: "Dashboard", url: "/", icon: LayoutDashboardIcon },
  { title: "Channels and sources", url: "/sources", icon: RssIcon },
  { title: "Rules", url: "/rules", icon: ListFilterIcon },
  { title: "Jobs and live recordings", url: "/jobs", icon: CircleDotIcon },
  { title: "Videos", url: "/videos", icon: FilmIcon },
]

export function navTitle(pathname: string): string {
  const match = navItems.find((item) =>
    item.url === "/" ? pathname === "/" : pathname.startsWith(item.url)
  )
  return match?.title ?? "savetube"
}
