import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { MoreHorizontalIcon, PlusIcon } from "lucide-react"
import { Link } from "react-router"

import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, mutations, type Rule, type Source, type SourceIn, type Tab } from "@/lib/api"
import { formatBytes, formatCount, formatDateTime } from "@/lib/format"

const TABS: { tab: Tab; label: string }[] = [
  { tab: "videos", label: "Videos" },
  { tab: "shorts", label: "Shorts" },
  { tab: "streams", label: "Live (past streams)" },
]

function AddSourceForm({ rules, onDone }: { rules: Rule[]; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<SourceIn>({
    url: "",
    tabs: ["videos", "shorts", "streams"],
    rule_id: rules[0]?.id ?? 0,
    poll_interval_minutes: 360,
  })
  const create = useMutation({
    mutationFn: mutations.createSources,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] })
      onDone()
    },
  })

  if (rules.length === 0) {
    return (
      <p className="px-4 text-sm">
        A source needs a rule.{" "}
        <Link className="underline" to="/rules">
          Create one first.
        </Link>
      </p>
    )
  }

  function toggleTab(tab: Tab, on: boolean) {
    setForm((f) => ({
      ...f,
      tabs: on
        ? TABS.map((t) => t.tab).filter((t) => t === tab || f.tabs.includes(t))
        : f.tabs.filter((t) => t !== tab),
    }))
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    create.mutate(form)
  }

  return (
    <form onSubmit={submit} className="flex flex-1 flex-col gap-4 overflow-y-auto px-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="source-url">Channel or playlist URL</Label>
        <Input
          id="source-url"
          required
          placeholder="https://www.youtube.com/@handle"
          value={form.url}
          onChange={(e) => setForm({ ...form, url: e.target.value })}
        />
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Channel tabs</legend>
        <p className="text-xs text-muted-foreground">
          One source per tab. Ignored for a playlist or a URL that already names a tab.
        </p>
        {TABS.map(({ tab, label }) => (
          <Label key={tab} className="font-normal">
            <Checkbox
              checked={form.tabs.includes(tab)}
              onCheckedChange={(on) => toggleTab(tab, on === true)}
            />
            {label}
          </Label>
        ))}
      </fieldset>
      <div className="flex flex-col gap-2">
        <Label htmlFor="source-rule">Rule</Label>
        <Select
          value={String(form.rule_id)}
          onValueChange={(v) => setForm({ ...form, rule_id: Number(v) })}
        >
          <SelectTrigger id="source-rule" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {rules.map((r) => (
              <SelectItem key={r.id} value={String(r.id)}>
                {r.name} ({r.types.join(", ")})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="source-interval">Poll every (minutes)</Label>
        <Input
          id="source-interval"
          type="number"
          min={10}
          max={10080}
          required
          value={form.poll_interval_minutes}
          onChange={(e) => setForm({ ...form, poll_interval_minutes: Number(e.target.value) })}
        />
      </div>
      {create.isError && <ErrorBox error={create.error} />}
      <SheetFooter className="px-0">
        <Button type="submit" disabled={create.isPending}>
          Add
        </Button>
      </SheetFooter>
    </form>
  )
}

function SourceActions({ source }: { source: Source }) {
  const queryClient = useQueryClient()
  const onSuccess = () => queryClient.invalidateQueries({ queryKey: ["sources"] })
  const poll = useMutation({ mutationFn: () => mutations.pollSource(source.id), onSuccess })
  const toggle = useMutation({
    mutationFn: () => mutations.updateSource(source.id, { enabled: !source.enabled }),
    onSuccess,
  })
  const remove = useMutation({ mutationFn: () => mutations.deleteSource(source.id), onSuccess })
  const error = poll.error ?? toggle.error ?? remove.error

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Source actions"
          title={error?.message}
          className={error ? "text-destructive" : undefined}
        >
          <MoreHorizontalIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem disabled={!source.enabled} onSelect={() => poll.mutate()}>
          Poll now
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => toggle.mutate()}>
          {source.enabled ? "Disable" : "Enable"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={() => {
            if (window.confirm(`Delete source ${source.url}? Stored videos stay.`)) remove.mutate()
          }}
        >
          Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function RuleSelect({ source, rules }: { source: Source; rules: Rule[] }) {
  const queryClient = useQueryClient()
  const update = useMutation({
    mutationFn: (rule_id: number) => mutations.updateSource(source.id, { rule_id }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources"] }),
  })
  return (
    <Select value={String(source.rule_id)} onValueChange={(v) => update.mutate(Number(v))}>
      <SelectTrigger size="sm" className="w-40" aria-label="Rule">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {rules.map((r) => (
          <SelectItem key={r.id} value={String(r.id)}>
            {r.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export function SourcesPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  // Polled, so a "Poll now" shows its result without a reload.
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources, refetchInterval: 15_000 })
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules })
  const [adding, setAdding] = useState(false)

  const channelName = new Map(channels.data?.map((c) => [c.id, c.name]))

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
      <PageSection
        title="Sources"
        actions={
          <Button size="sm" onClick={() => setAdding(true)}>
            <PlusIcon /> Add source
          </Button>
        }
      >
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
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={8} className="text-center text-muted-foreground">
                      No sources yet.
                    </TableCell>
                  </TableRow>
                )}
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
                    <TableCell>
                      <RuleSelect source={s} rules={rules.data ?? []} />
                    </TableCell>
                    <TableCell>every {s.poll_interval_minutes} min</TableCell>
                    <TableCell>{s.last_polled_at ? formatDateTime(s.last_polled_at) : "waiting"}</TableCell>
                    <TableCell className="max-w-80 truncate text-destructive" title={s.last_error ?? ""}>
                      {s.last_error ?? ""}
                    </TableCell>
                    <TableCell className="text-right">
                      <SourceActions source={s} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </QueryState>
      </PageSection>
      <Sheet open={adding} onOpenChange={setAdding}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Add source</SheetTitle>
            <SheetDescription>
              The scheduler lists the source within a minute and queues every video its rule
              keeps.
            </SheetDescription>
          </SheetHeader>
          {adding && <AddSourceForm rules={rules.data ?? []} onDone={() => setAdding(false)} />}
        </SheetContent>
      </Sheet>
    </Page>
  )
}
