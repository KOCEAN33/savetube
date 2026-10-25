import { useEffect, type CSSProperties } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, Outlet, useRouteError, useSearchParams } from "react-router"

import { AppSidebar } from "@/components/app-sidebar"
import { LoginWall } from "@/components/login-wall"
import { SiteHeader } from "@/components/site-header"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { api, ApiError, AUTH_ERROR_PARAM } from "@/lib/api"
import { queryKeys } from "@/lib/query-keys"

export function AppLayout() {
  const me = useQuery({ queryKey: queryKeys.me(), queryFn: api.me })
  const [search, setSearch] = useSearchParams()
  const signedIn = me.isSuccess

  // A repeated callback after a successful login lands here signed in, with an
  // error the user need not see.
  useEffect(() => {
    if (signedIn && search.has(AUTH_ERROR_PARAM)) {
      search.delete(AUTH_ERROR_PARAM)
      setSearch(search, { replace: true })
    }
  }, [signedIn, search, setSearch])

  if (me.error instanceof ApiError && me.error.status === 401) return <LoginWall />
  // The pages would only show their own "Not signed in" errors meanwhile.
  if (me.isPending) return null

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
        Back to Home
      </Link>
    </div>
  )
}
