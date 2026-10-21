import type { CSSProperties } from "react"
import { Link, Outlet, useRouteError } from "react-router"

import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"

export function AppLayout() {
  return (
    <SidebarProvider
      style={
        {
          "--sidebar-width": "calc(var(--spacing) * 72)",
          "--header-height": "calc(var(--spacing) * 12)",
        } as CSSProperties
      }
    >
      <AppSidebar variant="inset" />
      <SidebarInset>
        <SiteHeader />
        <div className="@container/main flex flex-1 flex-col">
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}

export function NotFound() {
  return <p className="p-6 text-sm text-muted-foreground">Page not found.</p>
}

// A page that throws while rendering shows this inside the layout, so the
// sidebar stays usable.
export function RouteError() {
  const error = useRouteError()
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="flex flex-col gap-2 p-6">
      <p className="text-sm font-medium">This page failed to load.</p>
      <p className="font-mono text-xs text-muted-foreground">{message}</p>
      <Link to="/" className="text-sm text-muted-foreground hover:underline">
        Back to the dashboard
      </Link>
    </div>
  )
}
