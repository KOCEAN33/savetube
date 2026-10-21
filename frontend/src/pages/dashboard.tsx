import { useQuery } from "@tanstack/react-query"
import { lazy, Suspense } from "react"

import { DataTable } from "@/components/data-table"
import { DownloadsCard } from "@/components/downloads-card"
import { ErrorBox } from "@/components/query-state"
import { SectionCards } from "@/components/section-cards"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

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

function Statistics() {
  const summary = useQuery({ queryKey: ["stats", "summary"], queryFn: api.statsSummary })
  const daily = useQuery({ queryKey: ["stats", "daily", 90], queryFn: () => api.statsDaily(90) })

  const error = summary.error ?? daily.error
  if (error) {
    return (
      <div className="px-4 lg:px-6">
        <ErrorBox error={error} />
      </div>
    )
  }
  if (!summary.data || !daily.data) {
    return (
      <div className="flex flex-col gap-6 px-4 lg:px-6">
        <Skeleton className="h-40 w-full" />
        <ChartAreaSkeleton />
      </div>
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
      <DataTable summary={summary.data} />
    </>
  )
}

export function DashboardPage() {
  return (
    <div className="flex flex-col gap-4 py-4 md:gap-6 md:py-6">
      <div className="px-4 lg:px-6">
        <DownloadsCard />
      </div>
      <Statistics />
    </div>
  )
}
