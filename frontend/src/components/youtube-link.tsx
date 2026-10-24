import { ExternalLinkIcon } from "lucide-react"

import { youtubeChannelUrl, youtubeVideoUrl } from "@/lib/channel"
import { cn } from "@/lib/utils"

// An external link to a page on YouTube, in a new tab. `name` is the video
// title or channel name. Icon-only, it labels the link for a screen reader.
function YouTubeLink({
  href,
  name,
  iconOnly,
  className,
}: {
  href: string
  name: string
  iconOnly: boolean
  className?: string
}) {
  return (
    <a
      className={cn(
        "inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline",
        className,
      )}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ${name} on YouTube`}
      aria-label={iconOnly ? `Open ${name} on YouTube` : undefined}
    >
      {!iconOnly && "YouTube"}
      <ExternalLinkIcon className="size-3" />
    </a>
  )
}

export function VideoYouTubeLink({
  id,
  title,
  iconOnly = false,
  className,
}: {
  id: string
  title?: string | null
  iconOnly?: boolean
  className?: string
}) {
  return (
    <YouTubeLink
      href={youtubeVideoUrl(id)}
      name={title ?? id}
      iconOnly={iconOnly}
      className={className}
    />
  )
}

export function ChannelYouTubeLink({
  id,
  name,
  className,
}: {
  id: string
  name?: string | null
  className?: string
}) {
  return (
    <YouTubeLink href={youtubeChannelUrl(id)} name={name ?? id} iconOnly className={className} />
  )
}
