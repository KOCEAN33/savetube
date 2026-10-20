import { useQuery } from "@tanstack/react-query"

import { ChartAreaInteractive } from "@/components/chart-area-interactive"
import { DataTable } from "@/components/data-table"
import { ErrorBox, Page } from "@/components/query-state"
import { SectionCards } from "@/components/section-cards"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

export function DashboardPage() {
  const summary = useQuery({ queryKey: ["stats", "summary"], queryFn: api.statsSummary })
  const daily = useQuery({ queryKey: ["stats", "daily", 90], queryFn: () => api.statsDaily(90) })

  const error = summary.error ?? daily.error
  if (error) {
    return (
      <Page>
        <ErrorBox error={error} />
      </Page>
    )
  }
  if (!summary.data || !daily.data) {
    return (
      <Page>
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-72 w-full" />
      </Page>
    )
  }

  return (
    <div className="flex flex-col gap-4 py-4 md:gap-6 md:py-6">
      <SectionCards summary={summary.data} daily={daily.data} />
      <div className="px-4 lg:px-6">
        <ChartAreaInteractive daily={daily.data} />
      </div>
      <DataTable summary={summary.data} />
    </div>
  )
}
