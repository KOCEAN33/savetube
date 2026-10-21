import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"

// The channel's profile picture, hotlinked from YouTube's image host. The
// fallback shows the first letter of the name until the image loads, or when
// no tab poll has found it yet.
export function ChannelAvatar({
  name,
  url,
  size = "default",
  className,
}: {
  name: string
  url: string | null
  size?: "default" | "sm" | "lg"
  className?: string
}) {
  return (
    <Avatar size={size} className={className}>
      {url && <AvatarImage src={url} alt="" referrerPolicy="no-referrer" />}
      <AvatarFallback>{name.slice(0, 1).toUpperCase()}</AvatarFallback>
    </Avatar>
  )
}
