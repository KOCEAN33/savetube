import { useState } from "react"

import { api, type VideoDetail } from "@/lib/api"
import { thumbUrl } from "@/lib/channel"

// Chrome plays the stored mkv as is. Safari answers "" and gets a note, because
// no transcode or remux exists.
const canPlayMkv = () => document.createElement("video").canPlayType("video/x-matroska") !== ""

function Note({ children }: { children: React.ReactNode }) {
  return <p className="rounded-lg border p-4 text-sm text-muted-foreground">{children}</p>
}

export function VideoPlayer({ video }: { video: VideoDetail }) {
  const [videoFailed, setVideoFailed] = useState(false)
  const [audioFailed, setAudioFailed] = useState(false)
  const poster = video.info?.thumbnail
  return (
    <div className="flex flex-col gap-3">
      {!canPlayMkv() ? (
        <Note>This browser cannot play the stored mkv file. Open this page in Chrome.</Note>
      ) : videoFailed ? (
        <Note>The video file could not be loaded.</Note>
      ) : (
        <video
          controls
          preload="metadata"
          src={api.mediaUrl(video.id)}
          poster={typeof poster === "string" ? poster : thumbUrl(video.id, "hq")}
          onError={() => setVideoFailed(true)}
          className="aspect-video w-full rounded-lg bg-black"
        />
      )}
      {!audioFailed ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs text-muted-foreground">Audio only</span>
          <audio
            controls
            preload="metadata"
            src={api.audioUrl(video.id)}
            onError={() => setAudioFailed(true)}
            className="h-9"
          />
        </div>
      ) : (
        <span className="text-xs text-muted-foreground">No audio copy stored.</span>
      )}
    </div>
  )
}
