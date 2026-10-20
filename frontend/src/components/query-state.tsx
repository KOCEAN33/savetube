import type { ReactNode } from "react"
import type { UseQueryResult } from "@tanstack/react-query"

import { Skeleton } from "@/components/ui/skeleton"

export function QueryState<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>
  children: (data: T) => ReactNode
}) {
  if (query.isPending) return <Skeleton className="h-32 w-full" />
  if (query.isError) return <ErrorBox error={query.error} />
  return <>{children(query.data)}</>
}

export function ErrorBox({ error }: { error: Error }) {
  return (
    <div className="rounded-lg border border-destructive/50 p-4 text-sm text-destructive">
      {error.message}
    </div>
  )
}

export function Page({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-6 px-4 py-4 md:py-6 lg:px-6">
      {children}
    </div>
  )
}

export function PageSection({
  title,
  actions,
  children,
}: {
  title: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{title}</h2>
        {actions}
      </div>
      <div className="overflow-hidden rounded-lg border">{children}</div>
    </section>
  )
}
