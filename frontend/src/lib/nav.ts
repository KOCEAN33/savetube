import {
  FilmIcon,
  LayoutDashboardIcon,
  RadioIcon,
  RssIcon,
  SettingsIcon,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  title: string
  url: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { title: "Dashboard", url: "/", icon: LayoutDashboardIcon },
  { title: "Channels", url: "/channels", icon: RssIcon },
  { title: "Live", url: "/live", icon: RadioIcon },
  { title: "Settings", url: "/settings", icon: SettingsIcon },
  { title: "Videos", url: "/videos", icon: FilmIcon },
]

export function navTitle(pathname: string): string {
  const match = navItems.find((item) =>
    item.url === "/" ? pathname === "/" : pathname.startsWith(item.url)
  )
  return match?.title ?? "savetube"
}
