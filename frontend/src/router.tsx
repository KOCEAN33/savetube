import { createBrowserRouter } from "react-router"

import { AppLayout, NotFound } from "@/components/app-layout"
import { DashboardPage } from "@/pages/dashboard"
import { JobsPage } from "@/pages/jobs"
import { RulesPage } from "@/pages/rules"
import { SourcesPage } from "@/pages/sources"
import { VideoDetailPage } from "@/pages/video-detail"
import { VideosPage } from "@/pages/videos"

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/sources", element: <SourcesPage /> },
      { path: "/rules", element: <RulesPage /> },
      { path: "/jobs", element: <JobsPage /> },
      { path: "/videos", element: <VideosPage /> },
      { path: "/videos/:id", element: <VideoDetailPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
])
