import { useState, type FormEvent } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { CornerDownRightIcon, MoreHorizontalIcon } from "lucide-react"

import { ErrorBox } from "@/components/query-state"
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
import { Skeleton } from "@/components/ui/skeleton"
import { api, mutations, type Channel, type Source, type SourceIn } from "@/lib/api"
import {
  channelBase,
  OTHER_CHANNELS_HINT,
  OTHER_CHANNELS_LABEL,
  sourceLabel,
  VIDEO_TABS,
} from "@/lib/channel"
import { formatDateTime } from "@/lib/format"
import { useInvalidate } from "@/pages/channel/use-invalidate"

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
  otherChannels,
}: {
  base: string
  open: boolean
  onOpenChange: (open: boolean) => void
  archiving: boolean
  interval: number
  otherChannels: boolean
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
        await mutations.createSources({
          url,
          archiving,
          poll_interval_minutes: interval,
          other_channels: otherChannels,
        })
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

export function ArchiveSection({ channel, sources }: { channel: Channel; sources: Source[] }) {
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
  // A `playlists` parent copies the flag to its children, so only top-level sources are set.
  const playlistSources = parent ? [parent, ...picks] : picks
  const otherOn = playlistSources.filter((s) => s.other_channels).length
  const otherMixed = otherOn > 0 && otherOn < playlistSources.length
  // Before the first playlist source the checkbox holds the choice for it, on
  // like the add-channel flow. After that a new playlist source follows the current value.
  const [firstOther, setFirstOther] = useState(true)
  const newOther = playlistSources.length === 0 ? firstOther : otherOn > 0

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
  const setOther = useMutation({
    mutationFn: async (other_channels: boolean) => {
      for (const s of playlistSources) await mutations.updateSource(s.id, { other_channels })
    },
    onSettled: invalidate,
  })
  const watch = useMutation({
    mutationFn: (watch_lives: boolean) => mutations.updateChannel(channel.id, { watch_lives }),
    onSettled: invalidate,
  })
  const busy = create.isPending || toggle.isPending || setAll.isPending || setOther.isPending
  const enabledTabs = VIDEO_TABS.filter(({ tab }) => tabSource(tab)?.enabled)
  const error = create.error ?? toggle.error ?? setAll.error ?? setOther.error ?? watch.error

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
                    other_channels: newOther,
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
          <div className="flex flex-col gap-1 pt-2">
            <Label className="font-normal">
              {playlistSources.length === 0 ? (
                <Checkbox
                  disabled={busy}
                  checked={firstOther}
                  onCheckedChange={(on) => setFirstOther(on === true)}
                />
              ) : (
                <Checkbox
                  disabled={busy}
                  checked={otherMixed ? "indeterminate" : otherOn > 0}
                  onCheckedChange={(on) => setOther.mutate(on === true)}
                />
              )}
              {OTHER_CHANNELS_LABEL}
            </Label>
            {playlistSources.length === 0 && (
              <p className="text-xs text-muted-foreground">
                Applies to the playlists you add with the buttons above.
              </p>
            )}
            {otherMixed && (
              <p className="text-xs text-muted-foreground">
                Mixed: {otherOn} of {playlistSources.length} playlist sources have it on.
                Changing it sets every one.
              </p>
            )}
            <p className="text-xs text-muted-foreground">{OTHER_CHANNELS_HINT}</p>
          </div>
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
        otherChannels={newOther}
      />
    </div>
  )
}
