import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CornerDownRightIcon, MoreHorizontalIcon, PlusIcon } from "lucide-react"
import { Link } from "react-router"

import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import {
  TitlePreview,
  WordListEditor,
  WordSuggestions,
} from "@/components/source-words"
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
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  api,
  mutations,
  type Channel,
  type Rule,
  type Source,
  type SourceIn,
  type Tab,
  type TitleWords,
} from "@/lib/api"
import { EXCLUDE_HINT, INCLUDE_HINT, addWord, type WordList } from "@/lib/words"
import { formatBytes, formatCount, formatDateTime } from "@/lib/format"

const TABS: { tab: Tab; label: string }[] = [
  { tab: "videos", label: "Videos" },
  { tab: "shorts", label: "Shorts" },
  { tab: "streams", label: "Live (past streams)" },
  { tab: "playlists", label: "All playlists, including ones added later" },
]


function OtherChannelsToggle({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <Label className="font-normal">
      <Checkbox checked={checked} onCheckedChange={(on) => onChange(on === true)} />
      Also archive videos from other channels in a playlist
    </Label>
  )
}

function PlaylistPicker({
  url,
  picked,
  onPickedChange,
}: {
  url: string
  picked: string[]
  onPickedChange: (urls: string[]) => void
}) {
  // The URL the listing ran for: editing the URL field must not refetch per keystroke.
  const [listedUrl, setListedUrl] = useState<string | null>(null)
  const playlists = useQuery({
    queryKey: ["channel-playlists", listedUrl],
    queryFn: () => api.channelPlaylists(listedUrl ?? ""),
    enabled: listedUrl !== null,
    // A failed listing already took seconds in yt-dlp; a retry triples the wait.
    retry: false,
  })

  function list() {
    onPickedChange([])
    setListedUrl(url.trim())
  }

  let body = null
  if (listedUrl !== null) {
    if (playlists.isPending)
      body = (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted-foreground">
            Listing the channel's playlists. This can take several seconds.
          </p>
          <Skeleton className="h-24 w-full" />
        </div>
      )
    else if (playlists.isError) body = <ErrorBox error={playlists.error} />
    else if (playlists.data.length === 0)
      body = <p className="text-xs text-muted-foreground">The channel has no playlists.</p>
    else
      body = (
        <ul className="flex max-h-72 flex-col gap-2 overflow-y-auto rounded-md border p-2">
          {playlists.data.map((p) => (
            <li key={p.id}>
              <Label className="font-normal">
                <Checkbox
                  disabled={p.source_id !== null}
                  checked={p.source_id !== null || picked.includes(p.url)}
                  onCheckedChange={(on) =>
                    onPickedChange(
                      on === true ? [...picked, p.url] : picked.filter((u) => u !== p.url),
                    )
                  }
                />
                <span className="min-w-0 flex-1 truncate" title={p.title}>
                  {p.title}
                </span>
                {p.source_id !== null && <Badge variant="outline">already a source</Badge>}
              </Label>
            </li>
          ))}
        </ul>
      )
  }

  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-2 text-sm font-medium">Pick playlists</legend>
      <p className="text-xs text-muted-foreground">
        One source per chosen playlist. A playlist the channel adds later is not picked up.
      </p>
      <Button
        type="button"
        variant="outline"
        className="w-fit"
        disabled={!url.trim() || playlists.isFetching}
        onClick={list}
      >
        List playlists
      </Button>
      {body}
    </fieldset>
  )
}

function AddSourceForm({ rules, onDone }: { rules: Rule[]; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<SourceIn>({
    url: "",
    tabs: ["videos", "shorts", "streams"],
    rule_id: rules[0]?.id ?? 0,
    poll_interval_minutes: 360,
    include_words: [],
    exclude_words: [],
    other_channels: false,
  })
  const [picked, setPicked] = useState<string[]>([])
  const create = useMutation({
    mutationFn: async (body: SourceIn) => {
      // A channel URL with no tab and some picked playlists means "only the picks".
      // A playlist URL ignores `tabs`, so it always goes through.
      if (body.tabs.length > 0 || picked.length === 0) await mutations.createSources(body)
      // One call per playlist URL, as the API takes one URL per request.
      for (const url of picked) await mutations.createSources({ ...body, url, tabs: [] })
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["sources"] }),
    onSuccess: onDone,
  })
  const tracksPlaylists =
    form.tabs.includes("playlists") || picked.length > 0 || form.url.includes("list=")

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
      <PlaylistPicker url={form.url} picked={picked} onPickedChange={setPicked} />
      <WordListEditor
        id="source-include"
        label="Include words"
        hint={INCLUDE_HINT}
        words={form.include_words}
        onChange={(include_words) => setForm({ ...form, include_words })}
      />
      <WordListEditor
        id="source-exclude"
        label="Exclude words"
        hint={EXCLUDE_HINT}
        words={form.exclude_words}
        onChange={(exclude_words) => setForm({ ...form, exclude_words })}
      />
      {tracksPlaylists && (
        <OtherChannelsToggle
          checked={form.other_channels}
          onChange={(other_channels) => setForm({ ...form, other_channels })}
        />
      )}
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

type WordTarget = "source" | "channel" | "global"

function ReadOnlyWords({ label, words }: { label: string; words: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">{label}:</span>
      {words.length === 0
        ? "none"
        : words.map((w) => (
            <Badge key={w} variant="outline">
              {w}
            </Badge>
          ))}
    </div>
  )
}

// Short name for a source: the playlist title, or "<channel> / <tab>" for a
// channel URL. The full URL stays on a second line.
function sourceLabel(s: Source, channelName: string | undefined): string {
  if (s.title) return s.title
  const url = new URL(s.url)
  const list = url.searchParams.get("list")
  if (list) return list
  const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent)
  const tab = parts.at(-1) ?? ""
  return `${channelName ?? parts.slice(0, -1).join("/")} / ${tab}`
}

const COPIED_TO_CHILDREN = "Copied to every playlist this source has added."

function EditSourceForm({
  source,
  parent,
  onDone,
}: {
  source: Source
  parent: Source | undefined
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  const globalWords = useQuery({ queryKey: ["words"], queryFn: api.globalWords })
  const channel = channels.data?.find((c) => c.id === source.channel_id)
  const [words, setWords] = useState<TitleWords>({
    include_words: source.include_words,
    exclude_words: source.exclude_words,
  })
  const [otherChannels, setOtherChannels] = useState(source.other_channels)
  const [target, setTarget] = useState<WordTarget>(source.channel_id ? "channel" : "source")
  const save = useMutation({
    // `start` sends the words and `archiving: true` in one PATCH, so downloads
    // never start under the stored words while the form holds newer ones.
    mutationFn: (start: boolean) =>
      mutations.updateSource(source.id, {
        ...words,
        other_channels: otherChannels,
        ...(start ? { archiving: true } : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] })
      onDone()
    },
  })
  // Channel and global lists save on every change, unlike the source's own
  // lists: the preview merges their stored state, not this form's.
  const saveChannel = useMutation({
    mutationFn: (body: TitleWords) => mutations.updateChannel(source.channel_id ?? "", body),
    onSuccess: (updated) => {
      queryClient.setQueryData<Channel[]>(["channels"], (cur) =>
        cur?.map((c) => (c.id === updated.id ? updated : c)),
      )
      queryClient.invalidateQueries({ queryKey: ["title-preview"] })
    },
  })
  const saveGlobal = useMutation({
    mutationFn: mutations.replaceGlobalWords,
    onSuccess: (updated) => {
      queryClient.setQueryData(["words"], updated)
      queryClient.invalidateQueries({ queryKey: ["title-preview"] })
    },
  })
  const channelWords: TitleWords | undefined = saveChannel.isPending
    ? saveChannel.variables
    : channel && { include_words: channel.include_words, exclude_words: channel.exclude_words }
  const writing = saveChannel.isPending || saveGlobal.isPending
  const present = new Map<string, WordList>()
  for (const lists of [globalWords.data, channelWords, parent, words]) {
    if (!lists) continue
    for (const list of ["include_words", "exclude_words"] as const) {
      for (const w of lists[list]) {
        if (!present.has(w.toLowerCase())) present.set(w.toLowerCase(), list)
      }
    }
  }

  function addSuggestion(word: string, list: WordList) {
    if (target === "source") {
      setWords((cur) => ({ ...cur, [list]: addWord(cur[list], word) }))
    } else if (target === "channel" && channelWords) {
      saveChannel.mutate({ ...channelWords, [list]: addWord(channelWords[list], word) })
    } else if (target === "global" && globalWords.data) {
      saveGlobal.mutate({ ...globalWords.data, [list]: addWord(globalWords.data[list], word) })
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate(false)
  }

  return (
    <form onSubmit={submit} className="flex flex-1 flex-col gap-4 overflow-y-auto px-4">
      <section className="flex flex-col gap-1 rounded-md border p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium">Global words</span>
          <Link className="text-xs underline" to="/words">
            Edit global words
          </Link>
        </div>
        {globalWords.isError ? (
          <ErrorBox error={globalWords.error} />
        ) : globalWords.data ? (
          <>
            <ReadOnlyWords label="Include" words={globalWords.data.include_words} />
            <ReadOnlyWords label="Exclude" words={globalWords.data.exclude_words} />
          </>
        ) : (
          <Skeleton className="h-10 w-full" />
        )}
      </section>
      {source.channel_id && (
        <section className="flex flex-col gap-4 rounded-md border p-3">
          <div className="flex flex-col gap-0.5">
            <span className="text-sm font-medium">
              Channel words{channel ? `: ${channel.name}` : ""}
            </span>
            <span className="text-xs text-muted-foreground">
              Apply to every source of this channel. Saved on each change.
            </span>
          </div>
          {channels.isError ? (
            <ErrorBox error={channels.error} />
          ) : channelWords ? (
            <>
              <WordListEditor
                id="channel-include"
                label="Include words"
                hint={INCLUDE_HINT}
                words={channelWords.include_words}
                disabled={writing}
                onChange={(include_words) => saveChannel.mutate({ ...channelWords, include_words })}
              />
              <WordListEditor
                id="channel-exclude"
                label="Exclude words"
                hint={EXCLUDE_HINT}
                words={channelWords.exclude_words}
                disabled={writing}
                onChange={(exclude_words) => saveChannel.mutate({ ...channelWords, exclude_words })}
              />
            </>
          ) : (
            <Skeleton className="h-16 w-full" />
          )}
        </section>
      )}
      {saveChannel.isError && <ErrorBox error={saveChannel.error} />}
      {saveGlobal.isError && <ErrorBox error={saveGlobal.error} />}
      {parent && (
        <section className="flex flex-col gap-1 rounded-md border p-3">
          <span className="text-sm font-medium">Parent words</span>
          <span className="truncate text-xs text-muted-foreground" title={parent.url}>
            From {parent.url}. Edit them on the parent.
          </span>
          <ReadOnlyWords label="Include" words={parent.include_words} />
          <ReadOnlyWords label="Exclude" words={parent.exclude_words} />
        </section>
      )}
      <section className="flex flex-col gap-4 rounded-md border p-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium">Source words</span>
          <span className="text-xs text-muted-foreground">
            Merged with the global, channel{parent ? ", and parent" : ""} words. Saved with the
            Save button.
          </span>
        </div>
        <WordListEditor
          id="edit-include"
          label="Include words"
          hint={INCLUDE_HINT}
          words={words.include_words}
          onChange={(include_words) => setWords({ ...words, include_words })}
        />
        <WordListEditor
          id="edit-exclude"
          label="Exclude words"
          hint={EXCLUDE_HINT}
          words={words.exclude_words}
          onChange={(exclude_words) => setWords({ ...words, exclude_words })}
        />
        {source.kind !== "tab" && (
          <OtherChannelsToggle checked={otherChannels} onChange={setOtherChannels} />
        )}
        {source.kind === "playlists" && (
          <p className="text-xs text-muted-foreground">
            The other-channels setting, rule, and poll interval: {COPIED_TO_CHILDREN}
          </p>
        )}
      </section>
      <WordSuggestions
        channelId={source.channel_id}
        onAdd={addSuggestion}
        disabled={writing}
        present={present}
        target={
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            aria-label="Add suggestions to"
            value={target}
            onValueChange={(v) => v && setTarget(v as WordTarget)}
          >
            <ToggleGroupItem value="source">This source</ToggleGroupItem>
            {source.channel_id && <ToggleGroupItem value="channel">This channel</ToggleGroupItem>}
            <ToggleGroupItem value="global">Global</ToggleGroupItem>
          </ToggleGroup>
        }
      />
      {source.kind === "playlists" ? (
        <p className="text-xs text-muted-foreground">
          This source lists playlists, not videos, so it has no preview. Each child playlist
          source has its own.
        </p>
      ) : (
        <TitlePreview sourceId={source.id} words={words} />
      )}
      {!source.archiving && (
        <div className="flex flex-col gap-2 rounded-md border p-3">
          <span className="text-sm font-medium">Listing only</span>
          <span className="text-xs text-muted-foreground">
            This source downloads nothing yet. Check the preview, then start.
            {source.kind === "playlists" && " Starting applies to all its playlists."}
          </span>
          <Button
            type="button"
            className="w-fit"
            disabled={save.isPending}
            onClick={() => save.mutate(true)}
          >
            Save and start downloads
          </Button>
        </div>
      )}
      {save.isError && <ErrorBox error={save.error} />}
      <SheetFooter className="px-0">
        <Button type="submit" disabled={save.isPending}>
          Save
        </Button>
      </SheetFooter>
    </form>
  )
}

function StartDownloadsButton({ source }: { source: Source }) {
  const queryClient = useQueryClient()
  const start = useMutation({
    mutationFn: () => mutations.updateSource(source.id, { archiving: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources"] }),
  })
  return (
    <Button
      variant="outline"
      size="xs"
      disabled={start.isPending}
      title={
        start.error?.message ??
        (source.kind === "playlists" ? "Starts downloads for all its playlists" : undefined)
      }
      className={start.isError ? "text-destructive" : undefined}
      onClick={() => start.mutate()}
    >
      Start downloads
    </Button>
  )
}

function SourceActions({
  source,
  label,
  onEdit,
}: {
  source: Source
  label: string
  onEdit: () => void
}) {
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
        <DropdownMenuItem onSelect={onEdit}>Edit title words</DropdownMenuItem>
        <DropdownMenuItem disabled={!source.enabled} onSelect={() => poll.mutate()}>
          Poll now
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => toggle.mutate()}>
          {source.enabled ? "Disable" : "Enable"}
        </DropdownMenuItem>
        {/* A child of "track all" cannot be deleted (409): disabling keeps its row, so the parent never re-adds it. */}
        {source.parent_id === null && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={() => {
                if (window.confirm(`Delete source "${label}"? Stored videos stay.`)) remove.mutate()
              }}
            >
              Delete
            </DropdownMenuItem>
          </>
        )}
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
      <SelectTrigger size="sm" className="w-32" aria-label="Rule">
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

// Each child follows its parent. A child whose parent is missing stays top level.
function groupByParent(rows: Source[]): { source: Source; childCount: number }[] {
  const ids = new Set(rows.map((s) => s.id))
  const children = new Map<number, Source[]>()
  for (const s of rows) {
    if (s.parent_id !== null && ids.has(s.parent_id)) {
      children.set(s.parent_id, [...(children.get(s.parent_id) ?? []), s])
    }
  }
  return rows
    .filter((s) => s.parent_id === null || !ids.has(s.parent_id))
    .flatMap((s) => {
      const kids = children.get(s.id) ?? []
      return [
        { source: s, childCount: kids.length },
        ...kids.map((k) => ({ source: k, childCount: 0 })),
      ]
    })
}

export function SourcesPage() {
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  // Polled, so a "Poll now" shows its result without a reload.
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources, refetchInterval: 15_000 })
  const rules = useQuery({ queryKey: ["rules"], queryFn: api.rules })
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)
  const [hideDisabledChildren, setHideDisabledChildren] = useState(false)
  const disabledChildren = sources.data?.filter((s) => s.parent_id !== null && !s.enabled).length ?? 0
  const [editingId, setEditingId] = useState<number | null>(null)
  const byId = new Map(sources.data?.map((s) => [s.id, s]))
  const editing = editingId === null ? undefined : byId.get(editingId)
  const parentOf = (s: Source) => (s.parent_id === null ? undefined : byId.get(s.parent_id))

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
      {added && (
        <div className="flex items-center justify-between gap-2 rounded-lg border p-3 text-sm">
          <span>
            Added. The new sources list first and download nothing until you press Start
            downloads. Set title words with Edit title words, then start.
          </span>
          <Button variant="ghost" size="sm" onClick={() => setAdded(false)}>
            Dismiss
          </Button>
        </div>
      )}
      <PageSection
        title="Sources"
        actions={
          <div className="flex items-center gap-4">
            {disabledChildren > 0 && (
              <Label className="font-normal">
                <Checkbox
                  checked={hideDisabledChildren}
                  onCheckedChange={(on) => setHideDisabledChildren(on === true)}
                />
                Hide disabled playlists ({disabledChildren})
              </Label>
            )}
            <Button size="sm" onClick={() => setAdding(true)}>
              <PlusIcon /> Add source
            </Button>
          </div>
        }
      >
        <QueryState query={sources}>
          {(rows) => (
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Source</TableHead>
                  <TableHead>Rule</TableHead>
                  <TableHead>Poll</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={4} className="text-center text-muted-foreground">
                      No sources yet.
                    </TableCell>
                  </TableRow>
                )}
                {groupByParent(rows)
                  .filter(({ source: s }) => !(hideDisabledChildren && s.parent_id !== null && !s.enabled))
                  .map(({ source: s, childCount }) => {
                    const label = sourceLabel(
                      s,
                      s.channel_id ? channelName.get(s.channel_id) : undefined,
                    )
                    return (
                      <TableRow key={s.id}>
                        {/* w-full with max-w-0 gives this column the leftover width and truncates inside it. */}
                        <TableCell className="w-full max-w-0">
                          <div className={s.parent_id !== null ? "flex gap-1 pl-4" : "flex gap-1"}>
                            {s.parent_id !== null && (
                              <CornerDownRightIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                            )}
                            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                              <div className="flex min-w-0 items-center gap-1">
                                <span className="truncate font-medium" title={label}>
                                  {label}
                                </span>
                                {!s.enabled && <Badge variant="outline">disabled</Badge>}
                                {s.enabled && parentOf(s)?.enabled === false && (
                                  <Badge variant="outline" title="Skipped while its parent is disabled">
                                    parent disabled
                                  </Badge>
                                )}
                                {!s.archiving && <Badge variant="secondary">listing only</Badge>}
                              </div>
                              <div className="flex min-w-0 gap-1 text-xs text-muted-foreground">
                                <span className="shrink-0">
                                  {s.kind}
                                  {s.kind === "playlists" && ` (${childCount} found)`}
                                </span>
                                <a
                                  className="truncate hover:underline"
                                  href={s.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  title={s.url}
                                >
                                  {s.url}
                                </a>
                              </div>
                              {s.last_error && (
                                <span className="truncate text-xs text-destructive" title={s.last_error}>
                                  {s.last_error}
                                </span>
                              )}
                            </div>
                          </div>
                        </TableCell>
                        <TableCell className="align-top">
                          <RuleSelect source={s} rules={rules.data ?? []} />
                          {s.kind === "playlists" && (
                            <p className="mt-1 text-xs text-muted-foreground" title={COPIED_TO_CHILDREN}>
                              Also sets its playlists
                            </p>
                          )}
                        </TableCell>
                        <TableCell className="align-top text-xs">
                          <div>every {s.poll_interval_minutes} min</div>
                          <div className="text-muted-foreground">
                            {s.last_polled_at ? formatDateTime(s.last_polled_at) : "waiting"}
                          </div>
                        </TableCell>
                        <TableCell className="align-top">
                          <div className="flex items-center justify-end gap-1">
                            {!s.archiving && <StartDownloadsButton source={s} />}
                            <SourceActions source={s} label={label} onEdit={() => setEditingId(s.id)} />
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
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
              The scheduler lists the source within a minute. It downloads nothing until you
              press Start downloads, so you can set title words against the preview first.
            </SheetDescription>
          </SheetHeader>
          {adding && (
            <AddSourceForm
              rules={rules.data ?? []}
              onDone={() => {
                setAdding(false)
                setAdded(true)
              }}
            />
          )}
        </SheetContent>
      </Sheet>
      <Sheet open={editing !== undefined} onOpenChange={(open) => !open && setEditingId(null)}>
        <SheetContent className="data-[side=right]:sm:max-w-xl">
          <SheetHeader>
            <SheetTitle className="truncate pr-6">
              {editing &&
                sourceLabel(
                  editing,
                  editing.channel_id ? channelName.get(editing.channel_id) : undefined,
                )}
            </SheetTitle>
            <SheetDescription className="truncate" title={editing?.url}>
              Title words for {editing?.url}
            </SheetDescription>
          </SheetHeader>
          {editing && (
            <EditSourceForm
              key={editing.id}
              source={editing}
              parent={parentOf(editing)}
              onDone={() => setEditingId(null)}
            />
          )}
        </SheetContent>
      </Sheet>
    </Page>
  )
}
