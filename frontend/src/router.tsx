import { createBrowserRouter } from "react-router"

import { AppLayout, NotFound } from "@/components/app-layout"
import { ChannelPage } from "@/pages/channel"
import { ChannelsPage } from "@/pages/channels"
import { DashboardPage } from "@/pages/dashboard"
import { LivePage } from "@/pages/live"
import { SettingsPage } from "@/pages/settings"
import { VideoDetailPage } from "@/pages/video-detail"
import { VideosPage } from "@/pages/videos"

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/channels", element: <ChannelsPage /> },
      { path: "/channels/:id", element: <ChannelPage /> },
      { path: "/live", element: <LivePage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/videos", element: <VideosPage /> },
      { path: "/videos/:id", element: <VideoDetailPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
])
