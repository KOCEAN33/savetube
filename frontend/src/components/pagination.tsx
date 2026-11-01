import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { formatCount } from "@/lib/format"

// The first and last page, the current one with a neighbour on each side, and a
// gap for each run of hidden pages. A gap of one page shows the page instead.
function pageItems(page: number, count: number): (number | "gap")[] {
  const shown = [...new Set([1, page - 1, page, page + 1, count])]
    .filter((n) => n >= 1 && n <= count)
    .sort((a, b) => a - b)
  const items: (number | "gap")[] = []
  for (const n of shown) {
    const prev = items.length ? (items[items.length - 1] as number) : 0
    if (n - prev === 2) items.push(prev + 1)
    else if (n - prev > 2) items.push("gap")
    items.push(n)
  }
  return items
}

// Previous, the numbered pages and next under a list, with the range it shows.
// Renders nothing while every item fits on one page.
export function Pagination({
  page,
  total,
  pageSize,
  onPage,
  label,
}: {
  page: number
  total: number
  pageSize: number
  onPage: (page: number) => void
  label: string
}) {
  const count = Math.ceil(total / pageSize)
  if (count <= 1) return null
  const first = (page - 1) * pageSize + 1
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t p-3">
      <span className="text-xs text-muted-foreground tabular-nums">
        {first > total
          ? `${formatCount(total)} total`
          : `${formatCount(first)}-${formatCount(Math.min(page * pageSize, total))} of ${formatCount(total)}`}
      </span>
      <nav aria-label={label} className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          disabled={page <= 1}
          aria-label="Previous page"
          onClick={() => onPage(Math.min(page - 1, count))}
        >
          <ChevronLeftIcon />
          <span className="hidden sm:inline">Previous</span>
        </Button>
        {pageItems(Math.min(page, count), count).map((item, i) =>
          item === "gap" ? (
            <span key={`gap-${i}`} aria-hidden className="w-7 text-center text-muted-foreground">
              …
            </span>
          ) : (
            <Button
              key={item}
              variant={item === page ? "outline" : "ghost"}
              size="icon-sm"
              className="w-auto min-w-7 px-1.5 tabular-nums"
              aria-label={`Page ${item}`}
              aria-current={item === page ? "page" : undefined}
              onClick={() => onPage(item)}
            >
              {item}
            </Button>
          ),
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={page >= count}
          aria-label="Next page"
          onClick={() => onPage(page + 1)}
        >
          <span className="hidden sm:inline">Next</span>
          <ChevronRightIcon />
        </Button>
      </nav>
    </div>
  )
}
