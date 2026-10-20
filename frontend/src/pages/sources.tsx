import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api } from "@/lib/api"
import { formatBytes, formatCount, formatDateTime } from "@/lib/format"

export function SourcesPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources })
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules })

  const channelName = new Map(channels.data?.map((c) => [c.id, c.name]))
  const ruleName = new Map(rules.data?.map((r) => [r.id, r.name]))

  return (
    <Page>
      <PageSection title="Channels">
        <QueryState query={channels}>
          {(rows) => (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Handle</TableHead>
                  <TableHead>Directory</TableHead>
                  <TableHead className="text-right">Videos</TableHead>
                  <TableHead className="text-right">Size</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-medium">
                      <Link
                        className="hover:underline"
                        to={`/videos?channel_id=${encodeURIComponent(c.id)}`}
                      >
                        {c.name}
                      </Link>
                    </TableCell>
                    <TableCell>{c.handle ?? "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{c.dir_name}</TableCell>
                    <TableCell className="text-right">
                      {formatCount(c.video_count)}
                    </TableCell>
                    <TableCell className="text-right">{formatBytes(c.bytes)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </QueryState>
      </PageSection>
      <PageSection title="Sources">
        <QueryState query={sources}>
          {(rows) => (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>URL</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Channel</TableHead>
                  <TableHead>Rule</TableHead>
                  <TableHead>Poll</TableHead>
                  <TableHead>Last sync</TableHead>
                  <TableHead>Last error</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell className="max-w-80 truncate">
                      <a className="hover:underline" href={s.url} target="_blank" rel="noreferrer">
                        {s.url}
                      </a>{" "}
                      {!s.enabled && <Badge variant="outline">disabled</Badge>}
                    </TableCell>
                    <TableCell>{s.kind}</TableCell>
                    <TableCell>
                      {s.channel_id ? (channelName.get(s.channel_id) ?? s.channel_id) : "-"}
                    </TableCell>
                    <TableCell>{ruleName.get(s.rule_id) ?? `#${s.rule_id}`}</TableCell>
                    <TableCell>every {s.poll_interval_minutes} min</TableCell>
                    <TableCell>{formatDateTime(s.last_polled_at)}</TableCell>
                    <TableCell className="max-w-80 truncate text-destructive" title={s.last_error ?? ""}>
                      {s.last_error ?? ""}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
