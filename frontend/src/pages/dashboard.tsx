import { useQuery } from "@tanstack/react-query"

import { ChartAreaInteractive } from "@/components/chart-area-interactive"
import { DataTable } from "@/components/data-table"
import { DownloadsCard } from "@/components/downloads-card"
import { ErrorBox } from "@/components/query-state"
import { SectionCards } from "@/components/section-cards"
import { SourcesWaitingCard } from "@/components/sources-waiting-card"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

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
        <Skeleton className="h-72 w-full" />
      </div>
    )
  }

  return (
    <>
      <SectionCards summary={summary.data} daily={daily.data} />
      <div className="px-4 lg:px-6">
        <ChartAreaInteractive daily={daily.data} />
      </div>
      <DataTable summary={summary.data} />
    </>
  )
}

export function DashboardPage() {
  return (
    <div className="flex flex-col gap-4 py-4 md:gap-6 md:py-6">
      {/* Empty while no source waits, so the gap above Downloads goes too. */}
      <div className="px-4 empty:hidden lg:px-6">
        <SourcesWaitingCard />
      </div>
      <div className="px-4 lg:px-6">
        <DownloadsCard />
      </div>
      <Statistics />
    </div>
  )
}
