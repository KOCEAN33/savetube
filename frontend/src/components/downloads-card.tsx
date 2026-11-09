import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"

import { RunningJob } from "@/components/job-row"
import { ErrorBox, QueryState } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { api, mutations } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatBytes } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { refetchMs } from "@/lib/refetch"

// Finished videos waiting on staging, and Move now, which the scheduler starts
// at once (docs/plan/storage.md, Staging).
function StagingLine() {
  const queryClient = useQueryClient()
  const staged = useQuery({
    queryKey: queryKeys.staging(),
    queryFn: api.staging,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const move = useMutation({
    mutationFn: mutations.moveStaged,
    onSuccess: (updated) => queryClient.setQueryData(queryKeys.staging(), updated),
  })
  if (!staged.data || staged.data.count === 0) return null
  const { count, bytes } = staged.data
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
      <span>
        {`${count} finished, waiting to move into the archive (${formatBytes(bytes)}).`}
        {move.isSuccess && " Moving into the archive."}
      </span>
      <Button
        size="sm"
        variant="outline"
        disabled={move.isPending}
        onClick={() => move.mutate()}
      >
        Move now
      </Button>
      {move.error && <ErrorBox error={move.error} />}
    </div>
  )
}

// Archive downloads across every channel: what runs now, how many wait, and
// the latest failed and interrupted runs no later run fixed. /downloads lists the rest.
export function DownloadsCard() {
  const active = useQuery({
    queryKey: queryKeys.runningJobs(),
    queryFn: () => api.jobs({ kind: "archive", state: ["running"] }),
    refetchInterval: (q) => refetchMs(Boolean(q.state.data?.length)),
  })
  const busy = (active.data?.length ?? 0) > 0
  const counts = useQuery({
    queryKey: queryKeys.archiveJobCounts("all"),
    queryFn: () => api.jobCounts({ kind: "archive" }),
    refetchInterval: refetchMs(busy),
  })
  const running = active.data ?? []

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Downloads</CardTitle>
        <CardDescription>
          {counts.data
            ? `${counts.data.running} running, ${counts.data.queued} queued`
            : "Loading"}
        </CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" asChild>
            <Link to="/downloads">View all</Link>
          </Button>
        </CardAction>
        <StagingLine />
      </CardHeader>
      <CardContent className="px-0">
        <QueryState query={active}>
          {() =>
            running.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">Nothing is downloading.</p>
            ) : (
              <ul className="flex flex-col divide-y">
                {running.map((j) => (
                  <RunningJob key={j.id} job={j} withChannel />
                ))}
              </ul>
            )
          }
        </QueryState>
      </CardContent>
    </Card>
  )
}
