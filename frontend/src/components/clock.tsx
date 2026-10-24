import { formatClock, formatDateTime } from "@/lib/format"

// The short time, with the full date and seconds on hover.
export function Clock({ iso }: { iso: string | null | undefined }) {
  return <span title={iso ? formatDateTime(iso) : undefined}>{formatClock(iso)}</span>
}
