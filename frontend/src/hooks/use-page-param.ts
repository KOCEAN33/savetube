import { useSearchParams } from "react-router"

// The 1-based page a list shows, held in the URL under `key` so back, forward
// and reload keep it. A page that is missing or not a whole number reads as 1.
export function usePageParam(key = "page"): [number, (page: number) => void] {
  const [params, setParams] = useSearchParams()
  const n = Number(params.get(key))
  const page = Number.isInteger(n) && n > 1 ? n : 1
  function setPage(next: number) {
    const updated = new URLSearchParams(params)
    if (next <= 1) updated.delete(key)
    else updated.set(key, String(next))
    setParams(updated)
  }
  return [page, setPage]
}
