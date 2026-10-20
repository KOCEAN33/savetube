import { useQuery } from "@tanstack/react-query"

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

function yesNo(value: boolean): string {
  return value ? "yes" : "no"
}

export function RulesPage() {
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules })

  return (
    <Page>
      <PageSection title="Rules">
        <QueryState query={rules}>
          {(rows) => (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Types</TableHead>
                  <TableHead>Uploaded after</TableHead>
                  <TableHead>Format</TableHead>
                  <TableHead>Cookies</TableHead>
                  <TableHead>Watch lives</TableHead>
                  <TableHead className="text-right">Max concurrent</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="font-medium">{r.name}</TableCell>
                    <TableCell className="flex flex-wrap gap-1">
                      {r.types.map((t) => (
                        <Badge key={t} variant="outline">
                          {t}
                        </Badge>
                      ))}
                    </TableCell>
                    <TableCell>{r.date_after ?? "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{r.format}</TableCell>
                    <TableCell>{yesNo(r.use_cookies)}</TableCell>
                    <TableCell>{yesNo(r.watch_lives)}</TableCell>
                    <TableCell className="text-right">{r.max_concurrent}</TableCell>
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
