import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ErrorBox, Page, PageSection, QueryState } from "@/components/query-state"
import { WordListEditor } from "@/components/source-words"
import { Button } from "@/components/ui/button"
import { api, mutations, type TitleWords } from "@/lib/api"
import { EXCLUDE_HINT, INCLUDE_HINT } from "@/lib/words"

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

export function WordsPage() {
  const words = useQuery({ queryKey: ["words"], queryFn: api.globalWords })
  return (
    <Page>
      <PageSection title="Global title words">
        <QueryState query={words}>{(data) => <GlobalWordsForm initial={data} />}</QueryState>
      </PageSection>
    </Page>
  )
}
