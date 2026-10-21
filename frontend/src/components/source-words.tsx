import { useState, type KeyboardEvent, type ReactNode } from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { MinusIcon, PlusIcon, XIcon } from "lucide-react"

import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { useDebounced } from "@/hooks/use-debounced"
import { api, type TitleWord, type TitleWords } from "@/lib/api"
import { MAX_WORD_LENGTH, MAX_WORDS, addWord, type WordList } from "@/lib/words"

export function WordListEditor({
  id,
  label,
  hint,
  words,
  onChange,
  disabled = false,
}: {
  id: string
  label: string
  hint: string
  words: string[]
  onChange: (words: string[]) => void
  disabled?: boolean
}) {
  const [draft, setDraft] = useState("")
  const full = words.length >= MAX_WORDS

  function add() {
    onChange(addWord(words, draft))
    setDraft("")
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault()
      add()
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      <p className="text-xs text-muted-foreground">{hint}</p>
      <div className="flex gap-2">
        <Input
          id={id}
          maxLength={MAX_WORD_LENGTH}
          disabled={disabled || full}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <Button
          type="button"
          variant="outline"
          disabled={disabled || full || !draft.trim()}
          onClick={add}
        >
          Add
        </Button>
      </div>
      {full && (
        <p className="text-xs text-destructive">
          At most {MAX_WORDS} words. Remove one to add another.
        </p>
      )}
      {words.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {words.map((w) => (
            <Badge key={w} variant="secondary" className="pr-0.5">
              {w}
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={`Remove ${w}`}
                className="size-4"
                disabled={disabled}
                onClick={() => onChange(words.filter((x) => x !== w))}
              >
                <XIcon />
              </Button>
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}

export function WordSuggestions({
  channelId,
  onAdd,
  disabled,
  target,
  present,
}: {
  channelId: string | null
  onAdd: (word: string, list: WordList) => void
  disabled: boolean
  target: ReactNode
  // Every word already in an effective list, keyed by its lowercase form.
  present: Map<string, WordList>
}) {
  const words = useQuery({
    queryKey: ["channel-words", channelId],
    queryFn: () => api.channelWords(channelId ?? ""),
    enabled: channelId !== null,
  })
  const empty = (
    <p className="text-xs text-muted-foreground">Suggestions appear after the first poll.</p>
  )

  let body = empty
  if (channelId !== null) {
    if (words.isPending) body = <Skeleton className="h-16 w-full" />
    else if (words.isError) body = <ErrorBox error={words.error} />
    else if (words.data.length > 0)
      body = <SuggestionChips words={words.data} onAdd={onAdd} disabled={disabled} present={present} />
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium">Suggested words</span>
        {target}
      </div>
      {body}
    </div>
  )
}

export function SuggestionChips({
  words,
  onAdd,
  disabled,
  present,
}: {
  words: TitleWord[]
  onAdd: (word: string, list: WordList) => void
  disabled: boolean
  // Every word already in an effective list, keyed by its lowercase form.
  present: Map<string, WordList>
}) {
  return (
    <ul className="flex flex-wrap gap-1">
      {words.map(({ word, count }) => {
        const list = present.get(word.toLowerCase())
        if (list)
          return (
            <li
              key={word}
              className="flex h-6 items-center gap-1 rounded-md border border-dashed px-2 text-xs text-muted-foreground"
            >
              <span>{word}</span>
              <span>{list === "include_words" ? "included" : "excluded"}</span>
            </li>
          )
        return (
          <li key={word} className="flex items-center gap-0.5 rounded-md border pl-2 text-xs">
            <span>{word}</span>
            <span className="text-muted-foreground">{count}</span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={`Include ${word}`}
              title="Include"
              disabled={disabled}
              onClick={() => onAdd(word, "include_words")}
            >
              <PlusIcon />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={`Exclude ${word}`}
              title="Exclude"
              disabled={disabled}
              onClick={() => onAdd(word, "exclude_words")}
            >
              <MinusIcon />
            </Button>
          </li>
        )
      })}
    </ul>
  )
}

// The body carries the source's own unsaved lists. The server merges the stored
// global and channel lists, so a save of either must invalidate ["title-preview"].
export function TitlePreview({ sourceId, words }: { sourceId: number; words: TitleWords }) {
  const body = useDebounced(words, 400)
  const preview = useQuery({
    queryKey: ["title-preview", sourceId, body],
    queryFn: () => api.titlePreview(sourceId, body),
    placeholderData: keepPreviousData,
  })

  let content
  if (preview.isPending) content = <Skeleton className="h-24 w-full" />
  else if (preview.isError) content = <ErrorBox error={preview.error} />
  else if (preview.data.total === 0)
    content = (
      <p className="text-xs text-muted-foreground">No titles yet. The preview fills after the first poll.</p>
    )
  else
    content = (
      <>
        <p className="text-sm">
          kept {preview.data.kept} of {preview.data.total}
        </p>
        <ul className="max-h-72 overflow-y-auto rounded-md border text-xs">
          {preview.data.titles.map((t) => (
            <li
              key={t.video_id}
              className={
                t.kept
                  ? "flex items-center gap-2 border-b px-2 py-1 last:border-b-0"
                  : "flex items-center gap-2 border-b px-2 py-1 text-muted-foreground last:border-b-0"
              }
            >
              <span className="min-w-0 flex-1 truncate" title={t.title}>
                {t.title}
              </span>
              {!t.kept && <Badge variant="outline">skipped</Badge>}
            </li>
          ))}
        </ul>
      </>
    )

  return (
    <div className="flex flex-col gap-2">
      <span className="text-sm font-medium">
        Preview{preview.isFetching && !preview.isPending ? " (updating)" : ""}
      </span>
      {content}
    </div>
  )
}

// Titles that are not stored, such as a channel scan's. The server merges the
// stored global lists, and the channel's when `channelId` names a stored one.
export function TitlesPreview({
  titles,
  words,
  channelId,
}: {
  titles: string[]
  words: TitleWords
  channelId?: string
}) {
  const body = useDebounced(words, 300)
  const preview = useQuery({
    queryKey: ["titles-preview", channelId, titles, body],
    queryFn: () => api.titlesPreview({ ...body, titles, channel_id: channelId }),
    placeholderData: keepPreviousData,
    enabled: titles.length > 0,
  })

  if (titles.length === 0)
    return <p className="text-xs text-muted-foreground">No titles to preview.</p>
  if (preview.isPending) return <Skeleton className="h-48 w-full" />
  if (preview.isError) return <ErrorBox error={preview.error} />
  const kept = preview.data.filter((t) => t.kept)
  const skipped = preview.data.filter((t) => !t.kept)
  return (
    <div className="grid min-h-0 gap-3 md:grid-cols-2">
      <TitleColumn label={`Kept (${kept.length})`} titles={kept} />
      <TitleColumn
        label={`Filtered out (${skipped.length})`}
        titles={skipped}
        muted
        updating={preview.isFetching}
      />
    </div>
  )
}

function TitleColumn({
  label,
  titles,
  muted = false,
  updating = false,
}: {
  label: string
  titles: { title: string }[]
  muted?: boolean
  updating?: boolean
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-sm font-medium">
        {label}
        {updating && <span className="ml-1 text-xs text-muted-foreground">(updating)</span>}
      </span>
      <ul
        className={
          muted
            ? "h-72 overflow-y-auto rounded-md border text-xs text-muted-foreground"
            : "h-72 overflow-y-auto rounded-md border text-xs"
        }
      >
        {titles.map((t, i) => (
          <li key={i} className="truncate border-b px-2 py-1 last:border-b-0" title={t.title}>
            {t.title}
          </li>
        ))}
      </ul>
    </div>
  )
}
