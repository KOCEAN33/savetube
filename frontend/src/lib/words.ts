export type WordList = "include_words" | "exclude_words"

// The backend answers 422 past these caps.
export const MAX_WORDS = 200
export const MAX_WORD_LENGTH = 100

// The backend dedupes on casefolded text too. Doing it here keeps the list on
// screen equal to what a save stores.

export function addWord(words: string[], word: string): string[] {
  const w = word.trim()
  if (!w || w.length > MAX_WORD_LENGTH || words.length >= MAX_WORDS) return words
  if (words.some((x) => x.toLowerCase() === w.toLowerCase())) return words
  return [...words, w]
}

export const INCLUDE_HINT = "Keep a title that contains any of these. Empty keeps every title."
export const EXCLUDE_HINT = "Skip a title that contains any of these."
