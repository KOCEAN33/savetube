import { useQuery } from "@tanstack/react-query"
import { lazy, Suspense, type ReactNode } from "react"

import { DataTable } from "@/components/data-table"
import { DownloadsCard } from "@/components/downloads-card"
import { ErrorBox } from "@/components/query-state"
import { RecordingCard } from "@/components/recorders"
import { SectionCards } from "@/components/section-cards"
import { SourcesWaitingCard } from "@/components/sources-waiting-card"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { queryKeys } from "@/lib/query-keys"

// recharts is large, so the chart is its own chunk and the cards render first.
const ChartAreaInteractive = lazy(() =>
  import("@/components/chart-area-interactive").then((m) => ({ default: m.ChartAreaInteractive })),
)

// Keep the box model in sync with ChartAreaInteractive so the layout does not
// jump: title line 22px, description line 20px, chart 250px.
function ChartAreaSkeleton() {
  return (
    <Card>
      <CardHeader>
        <Skeleton className="h-[22px] w-40" />
        <Skeleton className="h-5 w-32" />
      </CardHeader>
      <CardContent className="px-2 pt-4 sm:px-6 sm:pt-6">
        <Skeleton className="h-[250px] w-full" />
      </CardContent>
    </Card>
  )
}

// Downloads sits between the chart and the storage table, and still shows
// while the statistics load or fail.
function Statistics({ downloads }: { downloads: ReactNode }) {
  const summary = useQuery({ queryKey: queryKeys.statsSummary(), queryFn: api.statsSummary })
  const daily = useQuery({ queryKey: queryKeys.statsDaily(90), queryFn: () => api.statsDaily(90) })

  const error = summary.error ?? daily.error
  if (error) {
    return (
      <>
        <div className="px-4 lg:px-6">
          <ErrorBox error={error} />
        </div>
        {downloads}
      </>
    )
  }
  if (!summary.data || !daily.data) {
    return (
      <>
        <div className="flex flex-col gap-6 px-4 lg:px-6">
          <Skeleton className="h-40 w-full" />
          <ChartAreaSkeleton />
        </div>
        {downloads}
      </>
    )
  }

  return (
    <>
      <SectionCards summary={summary.data} daily={daily.data} />
      <div className="px-4 lg:px-6">
        <Suspense fallback={<ChartAreaSkeleton />}>
          <ChartAreaInteractive daily={daily.data} />
        </Suspense>
      </div>
      {downloads}
      <DataTable summary={summary.data} />
    </>
  )
}

export function DashboardPage() {
  return (
    <div className="flex flex-col gap-4 py-4 md:gap-6 md:py-6">
      {/* Empty while no source waits, so its gap goes too. */}
      <div className="px-4 empty:hidden lg:px-6">
        <SourcesWaitingCard />
      </div>
      {/* Empty while nothing records, so its gap goes too. */}
      <div className="px-4 empty:hidden lg:px-6">
        <RecordingCard />
      </div>
      <Statistics
        downloads={
          <div className="px-4 lg:px-6">
            <DownloadsCard />
          </div>
        }
      />
    </div>
  )
}
