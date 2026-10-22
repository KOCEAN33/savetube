import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"

import { DownloadPauseToggle } from "@/components/download-pause"
import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { WordListEditor } from "@/components/source-words"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  api,
  mutations,
  type Channel,
  type DownloadSettings,
  type CookieProfile,
  type DownloadSettingsIn,
  type TitleWords,
} from "@/lib/api"
import { formatDateTime } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"
import { COMMON_SUBTITLE_LANGS, getSubtitleLang, setSubtitleLang } from "@/lib/subtitles"
import { EXCLUDE_HINT, INCLUDE_HINT } from "@/lib/words"

const BYTES_PER_MB = 1_000_000
// A Select item needs a non-empty value, so "no profile" gets its own.
const NO_PROFILE = "__none__"
const SUBTITLES_OFF = "__off__"
const SUBTITLES_OTHER = "__other__"

interface FormState {
  max_concurrent: number
  rate_mb: string
  window_start: string
  window_end: string
  window_enabled: boolean
  daily_limit: string
  cookie_profile: string | null
}

function toForm(s: DownloadSettings): FormState {
  return {
    max_concurrent: s.max_concurrent,
    rate_mb: s.rate_limit_bytes === null ? "" : String(s.rate_limit_bytes / BYTES_PER_MB),
    window_start: s.window_start ?? "",
    window_end: s.window_end ?? "",
    window_enabled: s.window_enabled,
    daily_limit: s.daily_limit === null ? "" : String(s.daily_limit),
    cookie_profile: s.cookie_profile,
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
  if (f.window_enabled && start === null) return "Set the window start and end to use the window."
  let daily: number | null = null
  if (f.daily_limit.trim() !== "") {
    daily = Number(f.daily_limit)
    if (!Number.isInteger(daily) || daily < 1)
      return "Daily limit must be a whole number of at least 1, or empty."
  }
  return {
    max_concurrent: f.max_concurrent,
    rate_limit_bytes: rate,
    window_start: start,
    window_end: end,
    window_enabled: f.window_enabled,
    daily_limit: daily,
    cookie_profile: f.cookie_profile,
  }
}

function DownloadSettingsForm({
  settings,
  profiles,
}: {
  settings: DownloadSettings
  profiles: CookieProfile[]
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => toForm(settings))
  const [invalid, setInvalid] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: mutations.replaceDownloadSettings,
    onSuccess: (updated) => queryClient.setQueryData(queryKeys.downloadSettings(), updated),
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
      <div className="flex flex-col items-start gap-2">
        <Label>Pause</Label>
        <p className="text-xs text-muted-foreground">
          A pause stops running downloads and starts no new one until you resume. The page header
          holds the same switch.
        </p>
        <DownloadPauseToggle />
      </div>
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
      <div className="flex flex-col gap-2">
        <Label htmlFor="dl-daily">Daily limit (downloads per day, {settings.timezone})</Label>
        <p className="text-xs text-muted-foreground">
          Successful downloads since 00:00, plus running ones. A download still running when the
          limit is reached finishes. Empty means no limit.
        </p>
        <Input
          id="dl-daily"
          type="number"
          min={1}
          step={1}
          placeholder="unlimited"
          value={form.daily_limit}
          onChange={(e) => setForm({ ...form, daily_limit: e.target.value })}
        />
        <p className="text-xs text-muted-foreground">
          {settings.downloaded_today}
          {settings.daily_limit === null ? "" : ` / ${settings.daily_limit}`} today
        </p>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">Daily download window ({settings.timezone})</legend>
        <p className="text-xs text-muted-foreground">
          Downloads start only inside the window while it is on. A download still running at the
          window end finishes. Off keeps the times and allows any time. The window may cross
          midnight.
        </p>
        <div className="flex items-center gap-2">
          <Checkbox
            id="dl-window-enabled"
            checked={form.window_enabled}
            onCheckedChange={(on) => setForm({ ...form, window_enabled: on === true })}
          />
          <Label htmlFor="dl-window-enabled">Use the window</Label>
        </div>
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
      <div className="flex flex-col gap-2">
        <Label htmlFor="dl-cookie-profile">General cookie profile</Label>
        <p className="text-xs text-muted-foreground">
          Every poll, live check and download that is not members-only uses this profile. Use a
          throwaway account: a bot check costs that account at worst. Members-only videos use a
          profile whose membership a check confirmed, whatever this choice.
        </p>
        <Select
          value={form.cookie_profile ?? NO_PROFILE}
          onValueChange={(v) => setForm({ ...form, cookie_profile: v === NO_PROFILE ? null : v })}
        >
          <SelectTrigger id="dl-cookie-profile" className="w-64">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NO_PROFILE}>None (cookie-free)</SelectItem>
            {profiles.map((p) => (
              <SelectItem key={p.name} value={p.name}>
                {p.name}
              </SelectItem>
            ))}
            {form.cookie_profile !== null &&
              !profiles.some((p) => p.name === form.cookie_profile) && (
                <SelectItem value={form.cookie_profile}>{form.cookie_profile} (missing)</SelectItem>
              )}
          </SelectContent>
        </Select>
      </div>
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
      queryClient.setQueryData(queryKeys.words(), updated)
      queryClient.invalidateQueries({ queryKey: queryKeys.titlePreviews() })
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

function SubtitleLangForm() {
  const [lang, setLang] = useState(getSubtitleLang)
  const [other, setOther] = useState(
    () => lang !== null && !COMMON_SUBTITLE_LANGS.some(([code]) => code === lang),
  )

  function choose(next: string | null) {
    setLang(next)
    setSubtitleLang(next)
  }

  return (
    <div className="flex max-w-md flex-col gap-2 p-4">
      <Label htmlFor="subtitle-lang">Default subtitle language</Label>
      <p className="text-xs text-muted-foreground">
        A video opens with the subtitles in this language when it has them. The player&apos;s
        captions menu changes this default too. Stored in this browser only.
      </p>
      <Select
        value={other ? SUBTITLES_OTHER : (lang ?? SUBTITLES_OFF)}
        onValueChange={(v) => {
          setOther(v === SUBTITLES_OTHER)
          if (v !== SUBTITLES_OTHER) choose(v === SUBTITLES_OFF ? null : v)
        }}
      >
        <SelectTrigger id="subtitle-lang" className="w-64">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SUBTITLES_OFF}>Off</SelectItem>
          {COMMON_SUBTITLE_LANGS.map(([code, name]) => (
            <SelectItem key={code} value={code}>
              {name} ({code})
            </SelectItem>
          ))}
          <SelectItem value={SUBTITLES_OTHER}>Other code</SelectItem>
        </SelectContent>
      </Select>
      {other && (
        <Input
          aria-label="Subtitle language code"
          placeholder="yt-dlp language code, such as es"
          value={lang ?? ""}
          onChange={(e) => choose(e.target.value.trim() || null)}
        />
      )}
    </div>
  )
}

// Every membership result across channels, one column per present profile.
// The channel page shows the same results for one channel.
function MembershipTable({ channels, profiles }: { channels: Channel[]; profiles: CookieProfile[] }) {
  const checked = channels
    .filter((c) => c.memberships.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name))
  return (
    <div className="flex flex-col gap-3 p-4">
      <p className="text-xs text-muted-foreground">
        A poll that lists a members-only video checks each profile against its channel, at most
        once a day. A channel without a listed members-only video is never checked.
      </p>
      {checked.length === 0 ? (
        <p className="text-sm text-muted-foreground">No membership checked yet.</p>
      ) : (
        <Table>
          <TableHeader className="bg-muted">
            <TableRow>
              <TableHead>Channel</TableHead>
              {profiles.map((p) => (
                <TableHead key={p.name} className="font-mono">
                  {p.name}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {checked.map((c) => (
              <TableRow key={c.id}>
                <TableCell>
                  <Link className="hover:underline" to={`/channels/${encodeURIComponent(c.id)}`}>
                    {c.name}
                  </Link>
                </TableCell>
                {profiles.map((p) => {
                  const m = c.memberships.find((x) => x.profile === p.name)
                  return (
                    <TableCell key={p.name}>
                      {m ? (
                        <span className="flex items-center gap-2">
                          <Badge variant={m.is_member ? "default" : "secondary"}>
                            {m.is_member ? "member" : "not a member"}
                          </Badge>
                          <span className="text-xs text-muted-foreground">
                            {formatDateTime(m.checked_at)}
                          </span>
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">not checked</span>
                      )}
                    </TableCell>
                  )
                })}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

export function SettingsPage() {
  const settings = useQuery({ queryKey: queryKeys.downloadSettings(), queryFn: api.downloadSettings })
  const profiles = useQuery({ queryKey: queryKeys.cookieProfiles(), queryFn: api.cookieProfiles })
  const words = useQuery({ queryKey: queryKeys.words(), queryFn: api.globalWords })
  const channels = useQuery({ queryKey: queryKeys.channels(), queryFn: api.channels })
  return (
    <Page>
      <PageSection title="Global title words">
        <QueryState query={words}>{(data) => <GlobalWordsForm initial={data} />}</QueryState>
      </PageSection>
      <PageSection title="Playback">
        <SubtitleLangForm />
      </PageSection>
      <PageSection title="Download limits">
        <QueryState query={settings}>
          {(data) => (
            <QueryState query={profiles}>
              {(list) => <DownloadSettingsForm settings={data} profiles={list} />}
            </QueryState>
          )}
        </QueryState>
      </PageSection>
      <PageSection title="Memberships">
        <QueryState query={channels}>
          {(list) => (
            <QueryState query={profiles}>
              {(names) => <MembershipTable channels={list} profiles={names} />}
            </QueryState>
          )}
        </QueryState>
      </PageSection>
    </Page>
  )
}
