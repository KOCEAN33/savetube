import { useQuery } from "@tanstack/react-query"
import { BanIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { api } from "@/lib/api"
import { IDLE_REFETCH_MS } from "@/lib/constants"
import { formatClock, formatDateTime } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"

function future(iso: string | null | undefined): iso is string {
  return !!iso && new Date(iso).getTime() > Date.now()
}

// The pauses YouTube's refusals set: one for every archive download, and one
// per cookie profile, which holds back only that profile's downloads.
export function DownloadBlocks() {
  const settings = useQuery({
    queryKey: queryKeys.downloadSettings(),
    queryFn: api.downloadSettings,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const profiles = useQuery({
    queryKey: queryKeys.cookieProfiles(),
    queryFn: api.cookieProfiles,
    refetchInterval: IDLE_REFETCH_MS,
  })
  const blockedUntil = settings.data?.blocked_until
  const refused = (profiles.data ?? []).filter((p) => future(p.blocked_until))
  return (
    <>
      {future(blockedUntil) && (
        <Badge
          variant="destructive"
          title={`YouTube refused this client. Archive downloads wait until ${formatDateTime(blockedUntil)}.`}
        >
          <BanIcon />
          YouTube block until {formatClock(blockedUntil)}
        </Badge>
      )}
      {refused.map((p) => (
        <Badge
          key={p.name}
          variant="destructive"
          title={`googlevideo refused downloads with cookie profile ${p.name}. Its downloads wait until ${formatDateTime(p.blocked_until)}, or until its cookie file changes.`}
        >
          <BanIcon />
          {p.name} refused until {formatClock(p.blocked_until)}
        </Badge>
      ))}
    </>
  )
}
