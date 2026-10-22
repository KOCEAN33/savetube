import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"

import { ErrorBox } from "@/components/query-state"
import { TitlePreview, WordListEditor, WordSuggestions } from "@/components/source-words"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, mutations, type Channel, type Source, type TitleWords } from "@/lib/api"
import { sourceLabel } from "@/lib/channel"
import { EXCLUDE_HINT, INCLUDE_HINT, addWord, type WordList } from "@/lib/words"
import { useInvalidate } from "@/pages/channel/use-invalidate"

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

export function WordsSection({ channel, sources }: { channel: Channel; sources: Source[] }) {
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
