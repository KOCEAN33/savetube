import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckIcon } from "lucide-react"
import { useNavigate } from "react-router"

import { ChooseStep, type Choice } from "@/components/add-channel-dialog/choose-step"
import { ScanStep } from "@/components/add-channel-dialog/scan-step"
import { WordsStep } from "@/components/add-channel-dialog/words-step"
import { DateCutoffField, QualityField } from "@/components/channel-fields"
import { ErrorBox } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { mutations, type ChannelIn, type ChannelScan, type TitleWords } from "@/lib/api"
import { DEFAULT_FORMAT, VIDEO_TABS } from "@/lib/channel"
import { queryKeys } from "@/lib/query-keys"

const STEPS = ["Channel", "What to archive", "Title words", "Download settings"]

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
    otherChannels: true,
    watchLives: false,
  })
  const [words, setWords] = useState<TitleWords>({ include_words: [], exclude_words: [] })
  const [playlistsPass, setPlaylistsPass] = useState(false)
  const [dateAfter, setDateAfter] = useState<string | null>(null)
  const [format, setFormat] = useState(DEFAULT_FORMAT)
  const save = useMutation({
    mutationFn: mutations.createChannel,
    onSuccess: (channel) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.channels() })
      queryClient.invalidateQueries({ queryKey: queryKeys.sources() })
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
      otherChannels: true,
      watchLives: false,
    })
  }

  const playlistsChosen =
    choice.playlists === "all" || (choice.playlists === "some" && choice.picked.length > 0)
  const chosen = choice.tabs.length > 0 || playlistsChosen
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
      other_channels: choice.otherChannels,
      watch_lives: choice.watchLives,
      playlists_pass: playlistsChosen && playlistsPass,
      ...words,
      date_after: dateAfter,
      format,
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
          <WordsStep
            scan={scan}
            tabs={choice.tabs}
            words={words}
            onChange={setWords}
            playlistsPass={playlistsChosen ? playlistsPass : null}
            onPlaylistsPassChange={setPlaylistsPass}
          />
        )}
        {step === 3 && scan && (
          <div className="flex max-w-xl flex-col gap-6">
            <DateCutoffField id="new-date-after" value={dateAfter} onChange={setDateAfter} />
            <QualityField id="new-format" format={format} onChange={setFormat} />
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
              {(choice.playlists === "all" ||
                (choice.playlists === "some" && choice.picked.length > 0)) && (
                <span>
                  Videos other channels uploaded to these playlists:{" "}
                  {choice.otherChannels ? "download" : "skip"}
                </span>
              )}
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
