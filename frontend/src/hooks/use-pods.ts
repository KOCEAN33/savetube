import { useQuery } from "@tanstack/react-query"

import { api, type Pods, type RecorderJob } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { queryKeys } from "@/lib/query-keys"

export function usePods() {
  return useQuery({
    queryKey: queryKeys.pods(),
    queryFn: api.pods,
    refetchInterval: IDLE_REFETCH_MS,
  })
}

// A recorder Job carries its record job's id.
export function recorderOf(pods: Pods | undefined, jobId: number): RecorderJob | undefined {
  return pods?.recorders.find((r) => r.job_id === jobId)
}
