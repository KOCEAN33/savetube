import { useQuery } from "@tanstack/react-query"

import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { SuggestionChips, TitlesPreview, WordListEditor } from "@/components/source-words"
import { api, type ChannelScan, type TitleWords, type VideoTab } from "@/lib/api"
import { PLAYLISTS_PASS_HINT, VIDEO_TABS } from "@/lib/channel"
import { queryKeys } from "@/lib/query-keys"
import { EXCLUDE_HINT, INCLUDE_HINT, addWord, type WordList } from "@/lib/words"

export function WordsStep({
  scan,
  tabs,
  words,
  onChange,
  playlistsPass,
  onPlaylistsPassChange,
}: {
  scan: ChannelScan
  tabs: VideoTab[]
  words: TitleWords
  onChange: (words: TitleWords) => void
  // Null while no playlist is chosen: the checkbox stays hidden.
  playlistsPass: boolean | null
  onPlaylistsPassChange: (on: boolean) => void
}) {
  const globalWords = useQuery({ queryKey: queryKeys.words(), queryFn: api.globalWords })
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
        {playlistsPass !== null && (
          <div className="flex flex-col gap-2">
            <Label className="font-normal">
              <Checkbox
                checked={playlistsPass}
                onCheckedChange={(on) => onPlaylistsPassChange(on === true)}
              />
              Keep every video in the chosen playlists
            </Label>
            <p className="text-xs text-muted-foreground">{PLAYLISTS_PASS_HINT}</p>
          </div>
        )}
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
