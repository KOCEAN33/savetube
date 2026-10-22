import type { CookieProfile } from "@/lib/api"

// The profile name with the YouTube account its file signs in. Only the name
// while the profile list has not loaded.
export function profileLabel(name: string, profiles: CookieProfile[] | undefined): string {
  if (!profiles) return name
  const p = profiles.find((x) => x.name === name)
  if (!p?.account_name) return `${name} / account unknown`
  return `${name} / ${p.account_name}${p.account_handle ? ` (${p.account_handle})` : ""}`
}
