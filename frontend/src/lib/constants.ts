import type { JobKind, JobState } from "@/lib/api/types"

// Archive and replace jobs share the worker, the queue and the download limits.
export const DOWNLOAD_KINDS: JobKind[] = ["archive", "replace"]
// The worker writes progress every 5 s, so a faster refetch shows nothing new.
export const RUNNING_REFETCH_MS = 3_000
export const IDLE_REFETCH_MS = 15_000
// The states of a finished run. An interrupted run is a row of its own, and its
// job waits in the queue again.
export const FINISHED_JOB_STATES: JobState[] = ["succeeded", "failed", "interrupted", "cancelled"]
// Each job list shows one numbered page of this many jobs.
export const JOB_PAGE = 10
