import { createBrowserRouter, Navigate } from "react-router"

import { AppLayout, NotFound, RouteError } from "@/components/app-layout"

// Each page is its own chunk. The router loads it before the navigation
// commits, so no Suspense boundary is needed, and a failed load reaches RouteError.
export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      {
        errorElement: <RouteError />,
        children: [
          { path: "/", lazy: () => import("@/pages/dashboard").then((m) => ({ Component: m.DashboardPage })) },
          // The sidebar lists the channels. An old link to the list lands on Home.
          { path: "/channels", element: <Navigate to="/" replace /> },
          { path: "/channels/:id", lazy: () => import("@/pages/channel").then((m) => ({ Component: m.ChannelPage })) },
          { path: "/live", lazy: () => import("@/pages/live").then((m) => ({ Component: m.LivePage })) },
          { path: "/settings", lazy: () => import("@/pages/settings").then((m) => ({ Component: m.SettingsPage })) },
          { path: "/videos", lazy: () => import("@/pages/videos").then((m) => ({ Component: m.VideosPage })) },
          {
            path: "/videos/:id",
            lazy: () => import("@/pages/video-detail").then((m) => ({ Component: m.VideoDetailPage })),
          },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
])
