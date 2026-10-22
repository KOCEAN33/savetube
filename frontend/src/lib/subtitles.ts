// The subtitle language a video opens with is a per-browser viewing preference,
// so it lives in localStorage and not in the database.
const KEY = "savetube.subtitle-lang"

export const COMMON_SUBTITLE_LANGS: [code: string, name: string][] = [
  ["ja", "Japanese"],
  ["en", "English"],
  ["ko", "Korean"],
  ["zh-Hans", "Chinese (Simplified)"],
  ["zh-Hant", "Chinese (Traditional)"],
]

// null means off: a video opens with no subtitles showing.
export function getSubtitleLang(): string | null {
  return localStorage.getItem(KEY) || null
}

export function setSubtitleLang(lang: string | null) {
  if (lang) localStorage.setItem(KEY, lang)
  else localStorage.removeItem(KEY)
}
