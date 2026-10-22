import { IDLE_REFETCH_MS, RUNNING_REFETCH_MS } from "@/lib/constants"

// The refetch interval of a view that changes faster while a download runs.
export function refetchMs(running: boolean | undefined): number {
  return running ? RUNNING_REFETCH_MS : IDLE_REFETCH_MS
}
