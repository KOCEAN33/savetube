import {
  FilmIcon,
  HouseIcon,
  RadioIcon,
  SettingsIcon,
  type LucideIcon,
} from "lucide-react"

export interface NavItem {
  title: string
  url: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { title: "Home", url: "/", icon: HouseIcon },
  { title: "Live", url: "/live", icon: RadioIcon },
  { title: "Videos", url: "/videos", icon: FilmIcon },
  { title: "Settings", url: "/settings", icon: SettingsIcon },
]

export const channelHref = (id: string) => `/channels/${encodeURIComponent(id)}`

// The channel id of a channel page's path, or null on any other page.
export function channelIdOf(pathname: string): string | null {
  const prefix = "/channels/"
  return pathname.startsWith(prefix) ? decodeURIComponent(pathname.slice(prefix.length)) : null
}

export function navTitle(pathname: string): string {
  const match = navItems.find((item) =>
    item.url === "/" ? pathname === "/" : pathname.startsWith(item.url)
  )
  return match?.title ?? "savetube"
}
