import { useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { api, type VideoDetail } from "@/lib/api"
import { thumbUrl } from "@/lib/channel"
import { getSubtitleLang, setSubtitleLang } from "@/lib/subtitles"

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
  const subtitles = useQuery({
    queryKey: ["video", video.id, "subtitles"],
    queryFn: () => api.subtitles(video.id),
  })
  const [preferred] = useState(getSubtitleLang)
  const videoRef = useRef<HTMLVideoElement>(null)

  // The language picked in the native captions menu becomes the default, and
  // picking off turns the default off. A mode change from any other code would
  // count too, and none exists.
  useEffect(() => {
    const tracks = videoRef.current?.textTracks
    if (!tracks) return
    const remember = () =>
      setSubtitleLang(Array.from(tracks).find((t) => t.mode === "showing")?.language ?? null)
    tracks.addEventListener("change", remember)
    return () => tracks.removeEventListener("change", remember)
  }, [videoFailed])
  return (
    <div className="flex flex-col gap-3">
      {!canPlayMkv() ? (
        <Note>This browser cannot play the stored mkv file. Open this page in Chrome.</Note>
      ) : videoFailed ? (
        <Note>The video file could not be loaded.</Note>
      ) : (
        <video
          ref={videoRef}
          controls
          preload="metadata"
          src={api.mediaUrl(video.id)}
          poster={typeof poster === "string" ? poster : thumbUrl(video.id, "hq")}
          onError={() => setVideoFailed(true)}
          className="aspect-video w-full rounded-lg bg-black"
        >
          {subtitles.data?.map((s) => (
            <track
              key={s.lang}
              kind="subtitles"
              srcLang={s.lang}
              label={s.label}
              src={api.subtitleUrl(video.id, s.lang)}
              default={s.lang === preferred}
            />
          ))}
        </video>
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
