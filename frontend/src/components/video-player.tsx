import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { Clock } from "@/components/clock"
import { ErrorBox } from "@/components/query-state"
import { Button } from "@/components/ui/button"
import { useDismiss } from "@/hooks/use-dismiss"
import { api, mutations, type VideoDetail } from "@/lib/api"
import { thumbUrl } from "@/lib/channel"
import { queryKeys } from "@/lib/query-keys"
import { getSubtitleLang, setSubtitleLang } from "@/lib/subtitles"

// Chrome plays the stored mkv as is. Safari answers "" and gets a note, because
// no transcode or remux exists.
const canPlayMkv = () => document.createElement("video").canPlayType("video/x-matroska") !== ""

function Note({ children }: { children: React.ReactNode }) {
  return <p className="rounded-lg border p-4 text-sm text-muted-foreground">{children}</p>
}

const UNPLAYABLE: Record<string, string> = {
  pending: "Not downloaded yet.",
  downloading: "Downloading. It plays once the download finishes.",
  failed: "The download failed.",
  gone: "YouTube no longer serves this video.",
  deleted: "The files were deleted from the archive.",
}

// A staged video sits on staging until the scheduler moves it into the tree
// (docs/plan/storage.md, Staging). Move now moves every staged video.
function StagedNote() {
  const queryClient = useQueryClient()
  const move = useMutation({
    mutationFn: mutations.moveStaged,
    onSuccess: (updated) => queryClient.setQueryData(queryKeys.staging(), updated),
  })
  return (
    <div className="flex flex-col gap-2 rounded-lg border p-4 text-sm text-muted-foreground">
      <div className="flex flex-wrap items-center gap-2">
        <span>
          Downloaded, waiting to move into the archive. It plays after the move.
          {move.isSuccess && " Moving into the archive."}
        </span>
        <Button
          size="sm"
          variant="outline"
          disabled={move.isPending || move.isSuccess}
          onClick={() => move.mutate()}
        >
          Move now
        </Button>
      </div>
      {move.error && <ErrorBox error={move.error} />}
    </div>
  )
}

// The newest failed run of a video without a stored file: its reason and log
// in place, with the Retry and Dismiss of its row on the Downloads page.
function FailedNote({ id, state }: { id: string; state: string }) {
  const queryClient = useQueryClient()
  const failure = useQuery({
    queryKey: queryKeys.videoFailure(id),
    queryFn: () =>
      api.jobs({ video_id: id, state: ["failed", "interrupted", "missed"], limit: 1 }),
  })
  const retry = useMutation({
    mutationFn: mutations.retryJob,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs() })
      void queryClient.invalidateQueries({ queryKey: queryKeys.video(id) })
    },
  })
  const dismiss = useDismiss()
  const job = failure.data?.[0]
  if (!job) return <Note>{UNPLAYABLE[state]}</Note>
  return (
    <div className="flex flex-col gap-2 rounded-lg border p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">
          {UNPLAYABLE[state]} {job.reason ?? job.miss_reason?.replaceAll("_", " ")}
        </span>
        <span className="text-xs text-muted-foreground">
          <Clock iso={job.finished_at} />
        </span>
        {job.dismissed && <span className="text-xs text-muted-foreground">dismissed</span>}
        {job.retryable && (
          <Button
            size="sm"
            variant="outline"
            disabled={retry.isPending}
            onClick={() => retry.mutate(job.id)}
          >
            Retry
          </Button>
        )}
        {job.dismissable && (
          <Button
            size="sm"
            variant="ghost"
            disabled={dismiss.isPending}
            onClick={() => dismiss.mutate([{ kind: `${job.kind}_failed`, key: String(job.id) }])}
          >
            Dismiss
          </Button>
        )}
      </div>
      {job.log_tail && (
        <pre className="max-h-60 overflow-auto rounded bg-muted p-2 text-xs whitespace-pre-wrap break-all">
          {job.log_tail}
        </pre>
      )}
      {retry.error && <ErrorBox error={retry.error} />}
      {dismiss.error && <ErrorBox error={dismiss.error} />}
    </div>
  )
}

// Stands in for the player while the video is not `complete`.
export function UnplayableNote({ id, state }: { id: string; state: string }) {
  if (state === "staged") return <StagedNote />
  if (state === "failed" || state === "gone") return <FailedNote id={id} state={state} />
  return <Note>{UNPLAYABLE[state] ?? `The video is ${state}.`}</Note>
}

export function VideoPlayer({ video }: { video: VideoDetail }) {
  const [videoFailed, setVideoFailed] = useState(false)
  const [audioFailed, setAudioFailed] = useState(false)
  const poster = video.info?.thumbnail
  const subtitles = useQuery({
    queryKey: queryKeys.subtitles(video.id),
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
