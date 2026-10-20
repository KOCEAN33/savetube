import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { WordListEditor } from "@/components/source-words"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  api,
  mutations,
  type DownloadSettings,
  type DownloadSettingsIn,
  type TitleWords,
} from "@/lib/api"
import { EXCLUDE_HINT, INCLUDE_HINT } from "@/lib/words"

const BYTES_PER_MB = 1_000_000

interface FormState {
  max_concurrent: number
  rate_mb: string
  window_start: string
  window_end: string
}

function toForm(s: DownloadSettings): FormState {
  return {
    max_concurrent: s.max_concurrent,
    rate_mb: s.rate_limit_bytes === null ? "" : String(s.rate_limit_bytes / BYTES_PER_MB),
    window_start: s.window_start ?? "",
    window_end: s.window_end ?? "",
  }
}

// Returns the request body, or the message for the first rule the form breaks.
function toBody(f: FormState): DownloadSettingsIn | string {
  if (!Number.isInteger(f.max_concurrent) || f.max_concurrent < 1 || f.max_concurrent > 10)
    return "Max concurrent downloads must be 1 to 10."
  let rate: number | null = null
  if (f.rate_mb.trim() !== "") {
    const mb = Number(f.rate_mb)
    if (!Number.isFinite(mb) || mb <= 0) return "Rate limit must be a positive number, or empty."
    rate = Math.max(1, Math.round(mb * BYTES_PER_MB))
  }
  const start = f.window_start || null
  const end = f.window_end || null
  if ((start === null) !== (end === null))
    return "Set both window start and end, or leave both empty."
  if (start !== null && start === end) return "Window start and end must differ."
  return { max_concurrent: f.max_concurrent, rate_limit_bytes: rate, window_start: start, window_end: end }
}

function DownloadSettingsForm({ settings }: { settings: DownloadSettings }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => toForm(settings))
  const [invalid, setInvalid] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: mutations.replaceDownloadSettings,
    onSuccess: (updated) => queryClient.setQueryData(["download-settings"], updated),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    const body = toBody(form)
    if (typeof body === "string") {
      setInvalid(body)
      return
    }
    setInvalid(null)
    save.mutate(body)
  }

  return (
    <form onSubmit={submit} className="flex max-w-md flex-col gap-4 p-4">
      <p className="text-sm text-muted-foreground">
        These limits apply to archive downloads only. Live recordings ignore them.
      </p>
      <div className="flex flex-col gap-2">
        <Label htmlFor="dl-concurrent">Max concurrent downloads</Label>
        <Input
          id="dl-concurrent"
          type="number"
          min={1}
          max={10}
          required
          value={form.max_concurrent}
          onChange={(e) => setForm({ ...form, max_concurrent: Number(e.target.value) })}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="dl-rate">Rate limit (MB/s)</Label>
        <p className="text-xs text-muted-foreground">
          Per download: three downloads at once can use three times this rate. Empty means no limit.
        </p>
        <Input
          id="dl-rate"
          type="number"
          min={0}
          step="any"
          placeholder="unlimited"
          value={form.rate_mb}
          onChange={(e) => setForm({ ...form, rate_mb: e.target.value })}
        />
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Daily download window ({settings.timezone})</legend>
        <p className="text-xs text-muted-foreground">
          Downloads start only inside the window. A download still running at the window end
          finishes. Both empty means any time. The window may cross midnight.
        </p>
        <div className="flex items-center gap-2">
          <Input
            aria-label="Window start"
            type="time"
            value={form.window_start}
            onChange={(e) => setForm({ ...form, window_start: e.target.value })}
          />
          <span>to</span>
          <Input
            aria-label="Window end"
            type="time"
            value={form.window_end}
            onChange={(e) => setForm({ ...form, window_end: e.target.value })}
          />
        </div>
      </fieldset>
      {invalid && <ErrorBox error={new Error(invalid)} />}
      {save.isError && <ErrorBox error={save.error} />}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending}>
          Save
        </Button>
        {save.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
      </div>
    </form>
  )
}

function GlobalWordsForm({ initial }: { initial: TitleWords }) {
  const queryClient = useQueryClient()
  const [words, setWords] = useState(initial)
  const save = useMutation({
    mutationFn: mutations.replaceGlobalWords,
    onSuccess: (updated) => {
      queryClient.setQueryData(["words"], updated)
      queryClient.invalidateQueries({ queryKey: ["title-preview"] })
    },
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate(words)
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4 p-4">
      <p className="text-sm text-muted-foreground">
        Global words apply to every channel, merged with each channel's and source's own words.
      </p>
      <WordListEditor
        id="global-include"
        label="Include words"
        hint={INCLUDE_HINT}
        words={words.include_words}
        onChange={(include_words) => setWords({ ...words, include_words })}
      />
      <WordListEditor
        id="global-exclude"
        label="Exclude words"
        hint={EXCLUDE_HINT}
        words={words.exclude_words}
        onChange={(exclude_words) => setWords({ ...words, exclude_words })}
      />
      {save.isError && <ErrorBox error={save.error} />}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending}>
          Save
        </Button>
        {save.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
      </div>
    </form>
  )
}

export function SettingsPage() {
  const settings = useQuery({ queryKey: ["download-settings"], queryFn: api.downloadSettings })
  const words = useQuery({ queryKey: ["words"], queryFn: api.globalWords })
  return (
    <Page>
      <PageSection title="Global title words">
        <QueryState query={words}>{(data) => <GlobalWordsForm initial={data} />}</QueryState>
      </PageSection>
      <PageSection title="Download limits">
        <QueryState query={settings}>
          {(data) => <DownloadSettingsForm settings={data} />}
        </QueryState>
      </PageSection>
    </Page>
  )
}
