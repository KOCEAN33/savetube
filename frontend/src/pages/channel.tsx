import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CornerDownRightIcon, ExternalLinkIcon, MoreHorizontalIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import { CookiesField, DateCutoffField, QualityField } from "@/components/channel-fields"
import { JobsTable } from "@/components/jobs-table"
import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { TitlePreview, WordListEditor, WordSuggestions } from "@/components/source-words"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  api,
  mutations,
  type Channel,
  type ChannelSettings,
  type Source,
  type SourceIn,
  type TitleWords,
} from "@/lib/api"
import { channelBase, VIDEO_TABS } from "@/lib/channel"
import { formatBytes, formatCount, formatDateTime } from "@/lib/format"
import { EXCLUDE_HINT, INCLUDE_HINT, addWord, type WordList } from "@/lib/words"

// Short name for a source: the playlist title, or the tab.
function sourceLabel(s: Source): string {
  if (s.title) return s.title
  if (s.kind === "playlists") return "All playlists"
  const list = new URL(s.url).searchParams.get("list")
  if (list) return list
  const tab = s.url.slice(s.url.lastIndexOf("/") + 1)
  return VIDEO_TABS.find((t) => t.tab === tab)?.label ?? tab
}

function useInvalidate() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: ["sources"] })
    queryClient.invalidateQueries({ queryKey: ["channels"] })
  }
}

function SourceActions({ source }: { source: Source }) {
  const onSuccess = useInvalidate()
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
        {/* A child of "all playlists" cannot be deleted (409): disabling keeps its row, so the parent never adds it again. */}
        {source.parent_id === null && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={() => {
                if (window.confirm(`Delete "${sourceLabel(source)}"? Stored videos stay.`))
                  remove.mutate()
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

function SourceRow({ source, child = false }: { source: Source; child?: boolean }) {
  return (
    <li className="flex items-start gap-2 px-3 py-2">
      {child && <CornerDownRightIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 items-center gap-1">
          <a
            className="truncate text-sm font-medium hover:underline"
            href={source.url}
            target="_blank"
            rel="noreferrer"
            title={source.url}
          >
            {sourceLabel(source)}
          </a>
          {!source.enabled && <Badge variant="outline">disabled</Badge>}
          {!source.archiving && <Badge variant="secondary">paused</Badge>}
        </div>
        <span className="text-xs text-muted-foreground">
          every {source.poll_interval_minutes} min, last poll{" "}
          {source.last_polled_at ? formatDateTime(source.last_polled_at) : "waiting"}
        </span>
        {source.last_error && (
          <span className="truncate text-xs text-destructive" title={source.last_error}>
            {source.last_error}
          </span>
        )}
      </div>
      <SourceActions source={source} />
    </li>
  )
}

function PlaylistPickerDialog({
  base,
  open,
  onOpenChange,
  archiving,
  interval,
}: {
  base: string
  open: boolean
  onOpenChange: (open: boolean) => void
  archiving: boolean
  interval: number
}) {
  const invalidate = useInvalidate()
  const [picked, setPicked] = useState<string[]>([])
  const playlists = useQuery({
    queryKey: ["channel-playlists", base],
    queryFn: () => api.channelPlaylists(base),
    enabled: open,
    // A failed listing already took seconds in yt-dlp. A retry triples the wait.
    retry: false,
  })
  const add = useMutation({
    mutationFn: async () => {
      // One call per playlist URL, as the API takes one URL per request.
      for (const url of picked)
        await mutations.createSources({ url, archiving, poll_interval_minutes: interval })
    },
    onSettled: invalidate,
    onSuccess: () => {
      setPicked([])
      onOpenChange(false)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] flex-col sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Pick playlists</DialogTitle>
          <DialogDescription>
            One source per chosen playlist. A playlist the channel adds later is not picked up.
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {playlists.isPending ? (
            <Skeleton className="h-40 w-full" />
          ) : playlists.isError ? (
            <ErrorBox error={playlists.error} />
          ) : playlists.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">The channel has no playlists.</p>
          ) : (
            <ul className="flex flex-col gap-2 rounded-md border p-2">
              {playlists.data.map((p) => (
                <li key={p.id}>
                  <Label className="font-normal">
                    <Checkbox
                      disabled={p.source_id !== null}
                      checked={p.source_id !== null || picked.includes(p.url)}
                      onCheckedChange={(on) =>
                        setPicked(on === true ? [...picked, p.url] : picked.filter((u) => u !== p.url))
                      }
                    />
                    <span className="min-w-0 flex-1 truncate" title={p.title}>
                      {p.title}
                    </span>
                    {p.source_id !== null && <Badge variant="outline">already archived</Badge>}
                  </Label>
                </li>
              ))}
            </ul>
          )}
          {add.isError && <ErrorBox error={add.error} />}
        </div>
        <DialogFooter>
          <Button disabled={picked.length === 0 || add.isPending} onClick={() => add.mutate()}>
            Add {picked.length || ""} playlist{picked.length === 1 ? "" : "s"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ArchiveSection({ channel, sources }: { channel: Channel; sources: Source[] }) {
  const invalidate = useInvalidate()
  const base = channelBase(channel.id, sources)
  const top = sources.filter((s) => s.parent_id === null)
  const paused = top.length > 0 && top.every((s) => !s.archiving)
  const intervals = [...new Set(top.map((s) => s.poll_interval_minutes))]
  const [interval, setPollInterval] = useState(intervals[0] ?? 360)
  const [picking, setPicking] = useState(false)
  const tabSource = (tab: string) =>
    top.find((s) => s.kind === "tab" && s.url.endsWith(`/${tab}`))
  const parent = top.find((s) => s.kind === "playlists")
  const picks = top.filter((s) => s.kind === "playlist")
  const children = parent ? sources.filter((s) => s.parent_id === parent.id) : []

  const create = useMutation({
    mutationFn: (body: SourceIn) => mutations.createSources(body),
    onSettled: invalidate,
  })
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      mutations.updateSource(id, { enabled }),
    onSettled: invalidate,
  })
  const setAll = useMutation({
    // A `playlists` parent copies these to its children.
    mutationFn: async (body: { archiving?: boolean; poll_interval_minutes?: number }) => {
      for (const s of top) await mutations.updateSource(s.id, body)
    },
    onSettled: invalidate,
  })
  const watch = useMutation({
    mutationFn: (watch_lives: boolean) => mutations.updateChannel(channel.id, { watch_lives }),
    onSettled: invalidate,
  })
  const busy = create.isPending || toggle.isPending || setAll.isPending
  const enabledTabs = VIDEO_TABS.filter(({ tab }) => tabSource(tab)?.enabled)
  const error = create.error ?? toggle.error ?? setAll.error ?? watch.error

  function setTab(tab: string, on: boolean) {
    const existing = tabSource(tab)
    if (existing) toggle.mutate({ id: existing.id, enabled: on })
    else
      create.mutate({
        url: `${base}/${tab}`,
        archiving: !paused,
        poll_interval_minutes: interval,
      })
  }

  function saveInterval(e: FormEvent) {
    e.preventDefault()
    setAll.mutate({ poll_interval_minutes: interval })
  }

  return (
    <div className="flex flex-col gap-6 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium">
            {paused ? "Downloads are paused" : "Downloads are on"}
          </span>
          <span className="text-xs text-muted-foreground">
            {paused
              ? "Polls still list the channel and record what it holds. Nothing downloads."
              : "New videos that pass the words download after each poll."}
          </span>
        </div>
        <Button
          variant={paused ? "default" : "outline"}
          disabled={busy || top.length === 0}
          onClick={() => setAll.mutate({ archiving: paused })}
        >
          {paused ? "Resume downloads" : "Pause downloads"}
        </Button>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="flex flex-col gap-6">
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Tabs</legend>
            {VIDEO_TABS.map(({ tab, label }) => (
              <Label key={tab} className="font-normal">
                <Checkbox
                  disabled={busy}
                  checked={tabSource(tab)?.enabled ?? false}
                  onCheckedChange={(on) => setTab(tab, on === true)}
                />
                {label}
              </Label>
            ))}
            <p className="text-xs text-muted-foreground">
              Unchecking a tab disables it. Its stored videos stay.
            </p>
          </fieldset>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Live recording</legend>
            <Label className="font-normal">
              <Checkbox
                disabled={watch.isPending}
                checked={channel.watch_lives}
                onCheckedChange={(on) => watch.mutate(on === true)}
              />
              Record lives from their first second
            </Label>
            {channel.watch_lives && enabledTabs.length === 0 && (
              <p className="text-xs text-destructive">
                No tab is on, so live detection does not watch this channel.
              </p>
            )}
          </fieldset>
          <form onSubmit={saveInterval} className="flex flex-col gap-2">
            <Label htmlFor="channel-interval">Poll every (minutes)</Label>
            <div className="flex items-center gap-2">
              <Input
                id="channel-interval"
                type="number"
                min={10}
                max={10080}
                required
                className="w-32"
                value={interval}
                onChange={(e) => setPollInterval(Number(e.target.value))}
              />
              <Button type="submit" variant="outline" disabled={busy}>
                Save
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {intervals.length > 1
                ? `The sources differ now (${intervals.join(", ")} min). Saving sets every one.`
                : "Applies to every source of the channel."}
            </p>
          </form>
        </div>
        <fieldset className="flex min-w-0 flex-col gap-2">
          <legend className="mb-2 text-sm font-medium">Playlists</legend>
          <div className="flex flex-wrap gap-2">
            {!parent && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() =>
                  create.mutate({
                    url: base,
                    tabs: ["playlists"],
                    archiving: !paused,
                    poll_interval_minutes: interval,
                  })
                }
              >
                Track all playlists
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={() => setPicking(true)}>
              Pick playlists
            </Button>
          </div>
          {(parent || picks.length > 0) && (
            <ul className="flex max-h-96 flex-col divide-y overflow-y-auto rounded-md border">
              {parent && <SourceRow source={parent} />}
              {children.map((c) => (
                <SourceRow key={c.id} source={c} child />
              ))}
              {picks.map((s) => (
                <SourceRow key={s.id} source={s} />
              ))}
            </ul>
          )}
          {!parent && picks.length === 0 && (
            <p className="text-xs text-muted-foreground">No playlist is archived.</p>
          )}
        </fieldset>
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium">Tab sources</span>
        <ul className="flex flex-col divide-y rounded-md border">
          {top
            .filter((s) => s.kind === "tab")
            .map((s) => (
              <SourceRow key={s.id} source={s} />
            ))}
        </ul>
      </div>
      {error && <ErrorBox error={error} />}
      <PlaylistPickerDialog
        base={base}
        open={picking}
        onOpenChange={setPicking}
        archiving={!paused}
        interval={interval}
      />
    </div>
  )
}

function SettingsSection({ channel }: { channel: Channel }) {
  const invalidate = useInvalidate()
  const [form, setForm] = useState<Omit<ChannelSettings, "watch_lives">>({
    date_after: channel.date_after,
    format: channel.format,
    use_cookies: channel.use_cookies,
  })
  const save = useMutation({
    mutationFn: () => mutations.updateChannel(channel.id, form),
    onSuccess: invalidate,
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <form onSubmit={submit} className="flex max-w-xl flex-col gap-6 p-4">
      <DateCutoffField
        id="channel-date-after"
        value={form.date_after}
        onChange={(date_after) => setForm({ ...form, date_after })}
      />
      <QualityField
        id="channel-format"
        format={form.format}
        onChange={(format) => setForm({ ...form, format })}
      />
      <CookiesField
        checked={form.use_cookies}
        onChange={(use_cookies) => setForm({ ...form, use_cookies })}
      />
      {save.isError && <ErrorBox error={save.error} />}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending || !form.format.trim()}>
          Save
        </Button>
        {save.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
      </div>
    </form>
  )
}

type WordTarget = "channel" | "global"

function SourcePreview({ source }: { source: Source }) {
  const invalidate = useInvalidate()
  const [words, setWords] = useState<TitleWords>({
    include_words: source.include_words,
    exclude_words: source.exclude_words,
  })
  const save = useMutation({
    mutationFn: () => mutations.updateSource(source.id, words),
    onSuccess: invalidate,
  })
  const own = source.include_words.length + source.exclude_words.length > 0
  return (
    <div className="flex flex-col gap-4">
      <details className="rounded-md border p-3" open={own}>
        <summary className="cursor-pointer text-sm font-medium">
          Words for {sourceLabel(source)} only
        </summary>
        <div className="mt-3 flex flex-col gap-4">
          <WordListEditor
            id="source-include"
            label="Include words"
            hint={INCLUDE_HINT}
            words={words.include_words}
            onChange={(include_words) => setWords({ ...words, include_words })}
          />
          <WordListEditor
            id="source-exclude"
            label="Exclude words"
            hint={EXCLUDE_HINT}
            words={words.exclude_words}
            onChange={(exclude_words) => setWords({ ...words, exclude_words })}
          />
          {save.isError && <ErrorBox error={save.error} />}
          <Button
            type="button"
            variant="outline"
            className="w-fit"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            Save these words
          </Button>
        </div>
      </details>
      <TitlePreview sourceId={source.id} words={words} />
    </div>
  )
}

function WordsSection({ channel, sources }: { channel: Channel; sources: Source[] }) {
  const queryClient = useQueryClient()
  const globalWords = useQuery({ queryKey: ["words"], queryFn: api.globalWords })
  const [target, setTarget] = useState<WordTarget>("channel")
  const previewable = sources.filter((s) => s.kind !== "playlists")
  const [previewId, setPreviewId] = useState<number | null>(previewable[0]?.id ?? null)
  const previewed = previewable.find((s) => s.id === previewId) ?? previewable[0]
  // Channel and global lists save on every change: the preview merges their stored state.
  const saveChannel = useMutation({
    mutationFn: (body: TitleWords) => mutations.updateChannel(channel.id, body),
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
  const channelWords: TitleWords = saveChannel.isPending
    ? saveChannel.variables
    : { include_words: channel.include_words, exclude_words: channel.exclude_words }
  const writing = saveChannel.isPending || saveGlobal.isPending
  const present = new Map<string, WordList>()
  for (const lists of [globalWords.data, channelWords]) {
    if (!lists) continue
    for (const list of ["include_words", "exclude_words"] as const) {
      for (const w of lists[list]) {
        if (!present.has(w.toLowerCase())) present.set(w.toLowerCase(), list)
      }
    }
  }

  function addSuggestion(word: string, list: WordList) {
    if (target === "channel") {
      saveChannel.mutate({ ...channelWords, [list]: addWord(channelWords[list], word) })
    } else if (globalWords.data) {
      saveGlobal.mutate({ ...globalWords.data, [list]: addWord(globalWords.data[list], word) })
    }
  }

  return (
    <div className="grid gap-6 p-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <div className="flex flex-col gap-5">
        <WordListEditor
          id="channel-include"
          label="Include words"
          hint={`${INCLUDE_HINT} Saved on each change.`}
          words={channelWords.include_words}
          disabled={writing}
          onChange={(include_words) => saveChannel.mutate({ ...channelWords, include_words })}
        />
        <WordListEditor
          id="channel-exclude"
          label="Exclude words"
          hint={`${EXCLUDE_HINT} Saved on each change.`}
          words={channelWords.exclude_words}
          disabled={writing}
          onChange={(exclude_words) => saveChannel.mutate({ ...channelWords, exclude_words })}
        />
        {saveChannel.isError && <ErrorBox error={saveChannel.error} />}
        {saveGlobal.isError && <ErrorBox error={saveGlobal.error} />}
        <p className="text-xs text-muted-foreground">
          Global words also apply: include {globalWords.data?.include_words.join(", ") || "none"},
          exclude {globalWords.data?.exclude_words.join(", ") || "none"}.{" "}
          <Link className="underline" to="/settings">
            Edit them in Settings.
          </Link>
        </p>
        <WordSuggestions
          channelId={channel.id}
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
              <ToggleGroupItem value="channel">This channel</ToggleGroupItem>
              <ToggleGroupItem value="global">Global</ToggleGroupItem>
            </ToggleGroup>
          }
        />
      </div>
      <div className="flex min-w-0 flex-col gap-3">
        {previewed ? (
          <>
            <div className="flex items-center gap-2">
              <Label htmlFor="preview-source" className="shrink-0">
                Preview of
              </Label>
              <Select
                value={String(previewed.id)}
                onValueChange={(v) => setPreviewId(Number(v))}
              >
                <SelectTrigger id="preview-source" size="sm" className="w-64">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {previewable.map((s) => (
                    <SelectItem key={s.id} value={String(s.id)}>
                      {sourceLabel(s)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <SourcePreview key={previewed.id} source={previewed} />
          </>
        ) : (
          <p className="text-xs text-muted-foreground">No source to preview yet.</p>
        )}
      </div>
    </div>
  )
}

export function ChannelPage() {
  const { id = "" } = useParams()
  const channels = useQuery({ queryKey: ["channels"], queryFn: api.channels })
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources, refetchInterval: 15_000 })
  const jobs = useQuery({
    queryKey: ["jobs", "archive", id],
    queryFn: () => api.jobs({ kind: "archive", channel_id: id, limit: 200 }),
    refetchInterval: 15_000,
  })
  const channel = channels.data?.find((c) => c.id === id)
  const own = sources.data?.filter((s) => s.channel_id === id) ?? []

  if (channels.isPending || sources.isPending)
    return (
      <Page>
        <Skeleton className="h-64 w-full" />
      </Page>
    )
  if (channels.isError) return <Page><ErrorBox error={channels.error} /></Page>
  if (sources.isError) return <Page><ErrorBox error={sources.error} /></Page>
  if (!channel)
    return (
      <Page>
        <p className="text-sm text-muted-foreground">No channel {id}.</p>
      </Page>
    )

  return (
    <Page>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-baseline gap-2">
            <h2 className="text-2xl font-semibold">{channel.name}</h2>
            <a
              className="flex items-center gap-1 text-sm text-muted-foreground hover:underline"
              href={channelBase(channel.id, own)}
              target="_blank"
              rel="noreferrer"
            >
              {channel.handle ?? channel.id}
              <ExternalLinkIcon className="size-3.5" />
            </a>
          </div>
          <span className="font-mono text-xs text-muted-foreground">{channel.dir_name}</span>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span>{formatCount(channel.video_count)} videos</span>
          <span>{formatBytes(channel.bytes)}</span>
          <Button variant="outline" size="sm" asChild>
            <Link to={`/videos?channel_id=${encodeURIComponent(channel.id)}`}>Browse videos</Link>
          </Button>
        </div>
      </div>
      <PageSection title="What to archive">
        <ArchiveSection channel={channel} sources={own} />
      </PageSection>
      <PageSection title="Title words">
        <WordsSection channel={channel} sources={own} />
      </PageSection>
      <PageSection title="Download settings">
        <SettingsSection key={channel.id} channel={channel} />
      </PageSection>
      <PageSection title="Archive jobs">
        <QueryState query={jobs}>
          {(rows) => <JobsTable jobs={rows} empty="No archive job yet." />}
        </QueryState>
      </PageSection>
    </Page>
  )
}
