import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { type ChannelScan, type VideoTab } from "@/lib/api"
import {
  KEEP_ALL_HINT,
  KEEP_ALL_LABEL,
  OTHER_CHANNELS_HINT,
  OTHER_CHANNELS_LABEL,
  VIDEO_TABS,
} from "@/lib/channel"

type PlaylistMode = "none" | "all" | "some"

export interface Choice {
  tabs: VideoTab[]
  playlists: PlaylistMode
  picked: string[]
  otherChannels: boolean
  watchLives: boolean
  // The channel's `keep_all`. When on, the dialog skips the title words step.
  keepAll: boolean
}

export function ChooseStep({
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
          <legend className="mb-2 text-sm font-medium">Title words</legend>
          <Label className="font-normal">
            <Checkbox
              checked={choice.keepAll}
              onCheckedChange={(on) => onChange({ ...choice, keepAll: on === true })}
            />
            {KEEP_ALL_LABEL}
          </Label>
          <p className="text-xs text-muted-foreground">
            {KEEP_ALL_HINT} When checked, the next step skips title words.
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
        {choice.playlists !== "none" && (
          <div className="flex flex-col gap-1 pt-2">
            <Label className="font-normal">
              <Checkbox
                checked={choice.otherChannels}
                onCheckedChange={(on) => onChange({ ...choice, otherChannels: on === true })}
              />
              {OTHER_CHANNELS_LABEL}
            </Label>
            <p className="text-xs text-muted-foreground">{OTHER_CHANNELS_HINT}</p>
          </div>
        )}
      </fieldset>
    </div>
  )
}
