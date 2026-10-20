import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router"

import { Page, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { api, type VideoDetail } from "@/lib/api"
import {
  formatBytes,
  formatCount,
  formatDateTime,
  formatDuration,
  formatUploadDate,
} from "@/lib/format"

function infoString(info: VideoDetail["info"], key: string): string | null {
  const value = info[key]
  if (typeof value === "string") return value
  if (typeof value === "number") return formatCount(value)
  return null
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  )
}

export function VideoDetailPage() {
  const { id = "" } = useParams()
  const video = useQuery({ queryKey: ["video", id], queryFn: () => api.video(id) })

  return (
    <Page>
      <Link to="/videos" className="text-sm text-muted-foreground hover:underline">
        Back to videos
      </Link>
      <QueryState query={video}>
        {(v) => {
          const webpage = infoString(v.info, "webpage_url")
          const description = infoString(v.info, "description")
          return (
            <Card>
              <CardHeader>
                <CardTitle className="text-xl">{v.title}</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-6">
                <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
                  <Field label="ID">
                    <span className="font-mono">{v.id}</span>
                  </Field>
                  <Field label="Channel">{infoString(v.info, "channel") ?? v.channel_id}</Field>
                  <Field label="Type">{v.type}</Field>
                  <Field label="State">
                    <Badge variant="outline">{v.state}</Badge>
                  </Field>
                  <Field label="Uploaded">{formatUploadDate(v.upload_date)}</Field>
                  <Field label="Duration">{formatDuration(v.duration)}</Field>
                  <Field label="Size on disk">{formatBytes(v.size_bytes)}</Field>
                  <Field label="Completed">{formatDateTime(v.completed_at)}</Field>
                  <Field label="Views">{infoString(v.info, "view_count") ?? "-"}</Field>
                  <Field label="Resolution">{infoString(v.info, "resolution") ?? "-"}</Field>
                  <Field label="Format">{infoString(v.info, "format") ?? "-"}</Field>
                  <Field label="Source">
                    {webpage ? (
                      <a className="hover:underline" href={webpage} target="_blank" rel="noreferrer">
                        YouTube
                      </a>
                    ) : (
                      "-"
                    )}
                  </Field>
                </dl>
                {description && (
                  <p className="max-h-48 overflow-auto text-sm whitespace-pre-wrap text-muted-foreground">
                    {description}
                  </p>
                )}
                <details className="rounded-lg border">
                  <summary className="cursor-pointer p-3 text-sm font-medium">
                    Raw yt-dlp info
                  </summary>
                  <pre className="max-h-[32rem] overflow-auto border-t p-3 text-xs">
                    {JSON.stringify(v.info, null, 2)}
                  </pre>
                </details>
              </CardContent>
            </Card>
          )
        }}
      </QueryState>
    </Page>
  )
}
