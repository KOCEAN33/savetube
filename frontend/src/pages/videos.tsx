import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router"

import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type VideoType } from "@/lib/api"
import { formatBytes, formatCount, formatDuration, formatUploadDate } from "@/lib/format"

const PAGE_SIZE = 50
const ALL = "all"
const TYPES: VideoType[] = ["videos", "shorts", "lives", "members"]

export function VideosPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get("q") ?? ""
  const type = params.get("type") ?? undefined
  const channelId = params.get("channel_id") ?? undefined
  // From a channel page: its uploads and the videos its sources list.
  const listedBy = params.get("listed_by") ?? undefined
  const offset = Number(params.get("offset") ?? 0)

  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  const videos = useQuery({
    queryKey: ["videos", { q, type, channelId, listedBy, offset }],
    queryFn: () =>
      api.videos({ q, type, channel_id: channelId, listed_by: listedBy, limit: PAGE_SIZE, offset }),
    placeholderData: keepPreviousData,
  })
  const channelName = new Map(channels.data?.map((c) => [c.id, c.name]))

  function update(changes: Record<string, string | undefined>) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value === undefined || value === "" || value === ALL) next.delete(key)
      else next.set(key, value)
    }
    if (!("offset" in changes)) next.delete("offset")
    setParams(next)
  }

  const filters = (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        const value = new FormData(e.currentTarget).get("q")
        update({ q: typeof value === "string" ? value.trim() : undefined })
      }}
    >
      <Input
        key={q}
        name="q"
        defaultValue={q}
        placeholder="Search titles"
        className="h-8 w-56"
      />
      <Select value={type ?? ALL} onValueChange={(v) => update({ type: v })}>
        <SelectTrigger size="sm" className="w-32" aria-label="Type">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All types</SelectItem>
          {TYPES.map((t) => (
            <SelectItem key={t} value={t}>
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={channelId ?? listedBy ?? ALL}
        onValueChange={(v) => update({ channel_id: v, listed_by: undefined })}
      >
        <SelectTrigger size="sm" className="w-48" aria-label="Channel">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>All channels</SelectItem>
          {channels.data?.map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </form>
  )

  return (
    <Page>
      <PageSection title="Videos" actions={filters}>
        <QueryState query={videos}>
          {(page) => (
            <>
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>Title</TableHead>
                    <TableHead>Channel</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Uploaded</TableHead>
                    <TableHead className="text-right">Duration</TableHead>
                    <TableHead className="text-right">Size</TableHead>
                    <TableHead>State</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {page.items.map((v) => (
                    <TableRow key={v.id}>
                      <TableCell className="max-w-96 truncate font-medium">
                        <Link className="hover:underline" to={`/videos/${encodeURIComponent(v.id)}`}>
                          {v.title}
                        </Link>
                      </TableCell>
                      <TableCell>{channelName.get(v.channel_id) ?? v.channel_id}</TableCell>
                      <TableCell>{v.type}</TableCell>
                      <TableCell>{formatUploadDate(v.upload_date)}</TableCell>
                      <TableCell className="text-right">{formatDuration(v.duration)}</TableCell>
                      <TableCell className="text-right">{formatBytes(v.size_bytes)}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{v.state}</Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                  {page.items.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={7} className="h-24 text-center">
                        No videos.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
              <div className="flex items-center justify-between border-t p-3 text-sm">
                <span className="text-muted-foreground">
                  {page.total === 0
                    ? "0 videos"
                    : `${formatCount(offset + 1)}-${formatCount(offset + page.items.length)} of ${formatCount(page.total)}`}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => update({ offset: String(Math.max(offset - PAGE_SIZE, 0)) })}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + PAGE_SIZE >= page.total}
                    onClick={() => update({ offset: String(offset + PAGE_SIZE) })}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
