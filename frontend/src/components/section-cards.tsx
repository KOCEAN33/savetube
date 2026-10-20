import { TrendingUpIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardAction,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import type { DailyStat, StatsSummary } from "@/lib/api"
import { formatBytes, formatCount, formatHours, isoDay } from "@/lib/format"

function StatCard({
  label,
  value,
  badge,
  footer,
  detail,
}: {
  label: string
  value: string
  badge?: string
  footer: string
  detail: string
}) {
  return (
    <Card className="@container/card">
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl font-semibold tabular-nums @[250px]/card:text-3xl">
          {value}
        </CardTitle>
        {badge && (
          <CardAction>
            <Badge variant="outline">
              <TrendingUpIcon />
              {badge}
            </Badge>
          </CardAction>
        )}
      </CardHeader>
      <CardFooter className="flex-col items-start gap-1.5 text-sm">
        <div className="line-clamp-1 font-medium">{footer}</div>
        <div className="text-muted-foreground">{detail}</div>
      </CardFooter>
    </Card>
  )
}

export function SectionCards({
  summary,
  daily,
}: {
  summary: StatsSummary
  daily: DailyStat[]
}) {
  const cutoffDay = isoDay(7)
  const week = daily.filter((d) => d.day > cutoffDay)
  const weekBytes = week.reduce((sum, d) => sum + d.bytes, 0)
  const weekCount = week.reduce((sum, d) => sum + d.count, 0)
  const typeCounts = summary.by_type
    .map((t) => `${formatCount(t.count)} ${t.type}`)
    .join(", ")

  return (
    <div className="grid grid-cols-1 gap-4 px-4 *:data-[slot=card]:bg-gradient-to-t *:data-[slot=card]:from-primary/5 *:data-[slot=card]:to-card *:data-[slot=card]:shadow-xs lg:px-6 @xl/main:grid-cols-2 @5xl/main:grid-cols-4 dark:*:data-[slot=card]:bg-card">
      <StatCard
        label="Total stored"
        value={formatBytes(summary.total_bytes)}
        badge={`+${formatBytes(weekBytes)}`}
        footer={`${formatBytes(weekBytes)} added in the last 7 days`}
        detail="Includes subtitles and thumbnails"
      />
      <StatCard
        label="Videos"
        value={formatCount(summary.video_count)}
        badge={`+${formatCount(weekCount)}`}
        footer={`${formatCount(weekCount)} completed in the last 7 days`}
        detail={typeCounts || "No videos yet"}
      />
      <StatCard
        label="Hours archived"
        value={formatHours(summary.duration_seconds)}
        footer="Sum of video durations"
        detail={`Across ${formatCount(summary.by_channel.length)} channels`}
      />
      <StatCard
        label="Free space"
        value={formatBytes(summary.free_bytes)}
        footer="Dataset free space"
        detail={
          summary.free_bytes == null ? "Not sampled yet" : "Sampled hourly"
        }
      />
    </div>
  )
}
