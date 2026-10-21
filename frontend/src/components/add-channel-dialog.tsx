import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckIcon } from "lucide-react"
import { useNavigate } from "react-router"

import { ChannelAvatar } from "@/components/channel-avatar"
import { CookiesField, DateCutoffField, QualityField } from "@/components/channel-fields"
import { ErrorBox } from "@/components/query-state"
import { SuggestionChips, TitlesPreview, WordListEditor } from "@/components/source-words"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  api,
  mutations,
  type ChannelIn,
  type ChannelScan,
  type TitleWords,
  type VideoTab,
} from "@/lib/api"
import { DEFAULT_FORMAT, VIDEO_TABS } from "@/lib/channel"
import { EXCLUDE_HINT, INCLUDE_HINT, addWord, type WordList } from "@/lib/words"

const STEPS = ["Channel", "What to archive", "Title words", "Download settings"]

type PlaylistMode = "none" | "all" | "some"

interface Choice {
  tabs: VideoTab[]
  playlists: PlaylistMode
  picked: string[]
  watchLives: boolean
}

function Steps({ step }: { step: number }) {
  return (
    <ol className="flex flex-wrap items-center gap-2 text-xs">
      {STEPS.map((label, i) => (
        <li
          key={label}
          className={
            i === step
              ? "flex items-center gap-1.5 font-medium"
              : "flex items-center gap-1.5 text-muted-foreground"
          }
        >
          <span
            className={
              i < step
                ? "flex size-5 items-center justify-center rounded-full bg-primary text-primary-foreground"
                : i === step
                  ? "flex size-5 items-center justify-center rounded-full border-2 border-primary"
                  : "flex size-5 items-center justify-center rounded-full border"
            }
          >
            {i < step ? <CheckIcon className="size-3" /> : i + 1}
          </span>
          {label}
          {i < STEPS.length - 1 && <span className="mx-1 h-px w-6 bg-border" />}
        </li>
      ))}
    </ol>
  )
}

function ScanStep({
  url,
  onUrlChange,
  scan,
  onScanned,
}: {
  url: string
  onUrlChange: (url: string) => void
  scan: ChannelScan | null
  onScanned: (scan: ChannelScan) => void
}) {
  const run = useMutation({ mutationFn: api.channelScan, onSuccess: onScanned })

  function submit(e: FormEvent) {
    e.preventDefault()
    run.mutate(url.trim())
  }

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={submit} className="flex flex-col gap-2">
        <Label htmlFor="channel-url">Channel URL</Label>
        <div className="flex gap-2">
          <Input
            id="channel-url"
            required
            autoFocus
            placeholder="https://www.youtube.com/@handle"
            value={url}
            onChange={(e) => onUrlChange(e.target.value)}
          />
          <Button type="submit" disabled={!url.trim() || run.isPending}>
            {run.isPending ? "Scanning" : "Scan"}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          The scan lists the first page of the Videos, Shorts and Live tabs and the channel's
          playlists. It stores nothing.
        </p>
      </form>
      {run.isPending && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted-foreground">Listing the channel. This takes several seconds.</p>
          <Skeleton className="h-28 w-full" />
        </div>
      )}
      {run.isError && <ErrorBox error={run.error} />}
      {scan && !run.isPending && (
        <div className="flex flex-col gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2">
            <ChannelAvatar name={scan.name} url={scan.avatar_url} size="lg" />
            <span className="text-base font-medium">{scan.name}</span>
            {scan.handle && <span className="text-muted-foreground">{scan.handle}</span>}
            <span className="font-mono text-xs text-muted-foreground">{scan.channel_id}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {VIDEO_TABS.map(({ tab, label }) => (
              <Badge key={tab} variant="outline">
                {label}:{" "}
                {scan.tabs[tab] === null ? "no tab" : `${scan.tabs[tab]?.length ?? 0} on the first page`}
              </Badge>
            ))}
            <Badge variant="outline">Playlists: {scan.playlists.length}</Badge>
          </div>
        </div>
      )}
    </div>
  )
}

function ChooseStep({
  scan,
  choice,
  onChange,
}: {
  scan: ChannelScan
  choice: Choice
  onChange: (choice: Choice) => void
}) {
  function toggleTab(tab: VideoTab, on: boolean) {
    const tabs = on
      ? VIDEO_TABS.map((t) => t.tab).filter((t) => t === tab || choice.tabs.includes(t))
      : choice.tabs.filter((t) => t !== tab)
    onChange({ ...choice, tabs, watchLives: tabs.length > 0 && choice.watchLives })
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="flex flex-col gap-6">
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-sm font-medium">Tabs</legend>
          {VIDEO_TABS.map(({ tab, label }) => {
            const entries = scan.tabs[tab]
            return (
              <Label key={tab} className="font-normal">
                <Checkbox
                  disabled={entries === null}
                  checked={choice.tabs.includes(tab)}
                  onCheckedChange={(on) => toggleTab(tab, on === true)}
                />
                {label}
                <span className="text-xs text-muted-foreground">
                  {entries === null ? "the channel has no such tab" : `${entries.length} on the first page`}
                </span>
              </Label>
            )
          })}
          <p className="text-xs text-muted-foreground">
            A chosen tab archives everything on it, back to the date cutoff.
          </p>
        </fieldset>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-sm font-medium">Live recording</legend>
          <Label className="font-normal">
            <Checkbox
              disabled={choice.tabs.length === 0}
              checked={choice.watchLives}
              onCheckedChange={(on) => onChange({ ...choice, watchLives: on === true })}
            />
            Record lives from their first second
          </Label>
          <p className="text-xs text-muted-foreground">
            {choice.tabs.length === 0
              ? "Choose a tab first: live detection watches a channel through its tabs."
              : "The scheduler checks the channel every minute. A live is recorded when its title passes the words."}
          </p>
        </fieldset>
      </div>
      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Playlists</legend>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          aria-label="Playlists"
          value={choice.playlists}
          onValueChange={(v) => v && onChange({ ...choice, playlists: v as PlaylistMode })}
        >
          <ToggleGroupItem value="none">None</ToggleGroupItem>
          <ToggleGroupItem value="all" disabled={scan.playlists.length === 0}>
            All, including later ones
          </ToggleGroupItem>
          <ToggleGroupItem value="some" disabled={scan.playlists.length === 0}>
            Pick some
          </ToggleGroupItem>
        </ToggleGroup>
        {scan.playlists.length === 0 && (
          <p className="text-xs text-muted-foreground">The channel has no playlists.</p>
        )}
        {choice.playlists === "all" && (
          <p className="text-xs text-muted-foreground">
            Each playlist becomes its own source, and a playlist the channel adds later is picked
            up on the next poll.
          </p>
        )}
        {choice.playlists === "some" && (
          <ul className="flex max-h-80 flex-col gap-2 overflow-y-auto rounded-md border p-2">
            {scan.playlists.map((p) => (
              <li key={p.id}>
                <Label className="font-normal">
                  <Checkbox
                    checked={choice.picked.includes(p.url)}
                    onCheckedChange={(on) =>
                      onChange({
                        ...choice,
                        picked:
                          on === true
                            ? [...choice.picked, p.url]
                            : choice.picked.filter((u) => u !== p.url),
                      })
                    }
                  />
                  <span className="min-w-0 flex-1 truncate" title={p.title}>
                    {p.title}
                  </span>
                </Label>
              </li>
            ))}
          </ul>
        )}
      </fieldset>
    </div>
  )
}

function WordsStep({
  scan,
  tabs,
  words,
  onChange,
}: {
  scan: ChannelScan
  tabs: VideoTab[]
  words: TitleWords
  onChange: (words: TitleWords) => void
}) {
  const globalWords = useQuery({ queryKey: ["words"], queryFn: api.globalWords })
  // The chosen tabs' titles, or every scanned title when only playlists are chosen.
  const shown = tabs.length > 0 ? tabs : VIDEO_TABS.map((t) => t.tab)
  const titles = [
    ...new Set(shown.flatMap((t) => (scan.tabs[t] ?? []).map((e) => e.title))),
  ]
  const present = new Map<string, WordList>()
  for (const lists of [globalWords.data, words]) {
    if (!lists) continue
    for (const list of ["include_words", "exclude_words"] as const) {
      for (const w of lists[list]) {
        if (!present.has(w.toLowerCase())) present.set(w.toLowerCase(), list)
      }
    }
  }
  const global = globalWords.data
  const hasGlobal = global && (global.include_words.length > 0 || global.exclude_words.length > 0)

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <div className="flex flex-col gap-5">
        <WordListEditor
          id="new-include"
          label="Include words"
          hint={INCLUDE_HINT}
          words={words.include_words}
          onChange={(include_words) => onChange({ ...words, include_words })}
        />
        <WordListEditor
          id="new-exclude"
          label="Exclude words"
          hint={EXCLUDE_HINT}
          words={words.exclude_words}
          onChange={(exclude_words) => onChange({ ...words, exclude_words })}
        />
        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium">Suggested words</span>
          {scan.suggestions.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No word recurs in the scanned titles.
            </p>
          ) : (
            <SuggestionChips
              words={scan.suggestions}
              present={present}
              disabled={false}
              onAdd={(word, list) => onChange({ ...words, [list]: addWord(words[list], word) })}
            />
          )}
        </div>
        {hasGlobal && (
          <p className="text-xs text-muted-foreground">
            Global words also apply: include {global.include_words.join(", ") || "none"}, exclude{" "}
            {global.exclude_words.join(", ") || "none"}.
          </p>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          {titles.length} scanned titles from{" "}
          {shown.map((t) => VIDEO_TABS.find((v) => v.tab === t)?.label).join(", ")}. The preview
          updates as the words change.
        </p>
        <TitlesPreview titles={titles} words={words} />
      </div>
    </div>
  )
}

export function AddChannelDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[85vh] flex-col gap-0 p-0 sm:max-w-5xl">
        {/* Mounted only while open, so a reopened dialog starts at step 1. */}
        {open && <AddChannelSteps onDone={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  )
}

function AddChannelSteps({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [url, setUrl] = useState("")
  const [scan, setScan] = useState<ChannelScan | null>(null)
  const [choice, setChoice] = useState<Choice>({
    tabs: [],
    playlists: "none",
    picked: [],
    watchLives: false,
  })
  const [words, setWords] = useState<TitleWords>({ include_words: [], exclude_words: [] })
  const [dateAfter, setDateAfter] = useState<string | null>(null)
  const [format, setFormat] = useState(DEFAULT_FORMAT)
  const [useCookies, setUseCookies] = useState(false)
  const save = useMutation({
    mutationFn: mutations.createChannel,
    onSuccess: (channel) => {
      queryClient.invalidateQueries({ queryKey: ["channels"] })
      queryClient.invalidateQueries({ queryKey: ["sources"] })
      onDone()
      navigate(`/channels/${encodeURIComponent(channel.id)}`)
    },
  })

  function scanned(result: ChannelScan) {
    setScan(result)
    setChoice({
      tabs: VIDEO_TABS.map((t) => t.tab).filter((t) => result.tabs[t] !== null),
      playlists: "none",
      picked: [],
      watchLives: false,
    })
  }

  const chosen =
    choice.tabs.length > 0 ||
    choice.playlists === "all" ||
    (choice.playlists === "some" && choice.picked.length > 0)
  const canNext = [scan !== null, chosen, true, false][step]

  function submit() {
    if (!scan) return
    const body: ChannelIn = {
      url: scan.url,
      channel_id: scan.channel_id,
      handle: scan.handle,
      name: scan.name,
      avatar_url: scan.avatar_url,
      tabs: choice.tabs,
      all_playlists: choice.playlists === "all",
      playlists:
        choice.playlists === "some"
          ? scan.playlists.filter((p) => choice.picked.includes(p.url))
          : [],
      watch_lives: choice.watchLives,
      ...words,
      date_after: dateAfter,
      format,
      use_cookies: useCookies,
    }
    save.mutate(body)
  }

  return (
    <>
      <DialogHeader className="gap-3 border-b p-6 pb-4">
        <DialogTitle className="text-lg">
          Add channel{scan && step > 0 ? `: ${scan.name}` : ""}
        </DialogTitle>
        <DialogDescription>
          From a URL to downloading in four steps. Downloads start on the next scheduler tick
          after saving.
        </DialogDescription>
        <Steps step={step} />
      </DialogHeader>
      <div className="min-h-[24rem] flex-1 overflow-y-auto p-6">
        {step === 0 && (
          <ScanStep
            url={url}
            onUrlChange={setUrl}
            scan={scan}
            onScanned={scanned}
          />
        )}
        {step === 1 && scan && <ChooseStep scan={scan} choice={choice} onChange={setChoice} />}
        {step === 2 && scan && (
          <WordsStep scan={scan} tabs={choice.tabs} words={words} onChange={setWords} />
        )}
        {step === 3 && scan && (
          <div className="flex max-w-xl flex-col gap-6">
            <DateCutoffField id="new-date-after" value={dateAfter} onChange={setDateAfter} />
            <QualityField id="new-format" format={format} onChange={setFormat} />
            <CookiesField checked={useCookies} onChange={setUseCookies} />
            <div className="flex flex-col gap-1 rounded-lg border p-4 text-xs text-muted-foreground">
              <span className="text-sm font-medium text-foreground">Summary</span>
              <span>
                Tabs:{" "}
                {choice.tabs
                  .map((t) => VIDEO_TABS.find((v) => v.tab === t)?.label)
                  .join(", ") || "none"}
              </span>
              <span>
                Playlists:{" "}
                {choice.playlists === "all"
                  ? "all, including later ones"
                  : choice.playlists === "some"
                    ? `${choice.picked.length} picked`
                    : "none"}
              </span>
              <span>Live recording: {choice.watchLives ? "on" : "off"}</span>
              <span>
                Words: include {words.include_words.join(", ") || "any"}, exclude{" "}
                {words.exclude_words.join(", ") || "none"}
              </span>
            </div>
            {save.isError && <ErrorBox error={save.error} />}
          </div>
        )}
      </div>
      <DialogFooter className="m-0 rounded-b-xl px-6">
        {step > 0 && (
          <Button variant="outline" onClick={() => setStep(step - 1)} disabled={save.isPending}>
            Back
          </Button>
        )}
        {step < STEPS.length - 1 ? (
          <Button onClick={() => setStep(step + 1)} disabled={!canNext}>
            Next
          </Button>
        ) : (
          <Button onClick={submit} disabled={save.isPending || !format.trim()}>
            {save.isPending ? "Saving" : "Save and start downloads"}
          </Button>
        )}
      </DialogFooter>
    </>
  )
}
