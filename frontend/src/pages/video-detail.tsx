import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Trash2Icon } from "lucide-react"
import { Link, useNavigate, useParams } from "react-router"

import { Clock } from "@/components/clock"
import { FormatWatchCard } from "@/components/format-watch"
import { ErrorBox, Page, QueryState } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { VideoPlayer } from "@/components/video-player"
import { ChannelYouTubeLink, VideoYouTubeLink } from "@/components/youtube-link"
import { api, mutations, type VideoDetail } from "@/lib/api"
import { formatBytes, formatCount, formatDuration, formatUploadDate } from "@/lib/format"
import { queryKeys } from "@/lib/query-keys"

function infoString(info: VideoDetail["info"], key: string): string | null {
  const value = info?.[key]
  if (typeof value === "string") return value
  if (typeof value === "number") return formatCount(value)
  return null
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  )
}

// Deletes the stored files after a confirm. The scheduler refuses (409) while a
// job or an encode holds the video, and the dialog shows its reason.
function DeleteVideoButton({ video }: { video: VideoDetail }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const remove = useMutation({
    mutationFn: () => mutations.deleteVideo(video.id),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.video(video.id) })
      void queryClient.invalidateQueries({ queryKey: ["videos"] })
      void queryClient.invalidateQueries({ queryKey: ["stats"] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.channels() })
      // Back to the list the user came from, else the Videos page.
      if ((window.history.state as { idx?: number } | null)?.idx) void navigate(-1)
      else void navigate("/videos", { replace: true })
    },
  })

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
        <Trash2Icon />
        Delete
      </Button>
      <Dialog
        open={confirming}
        onOpenChange={(open) => {
          setConfirming(open)
          if (!open) remove.reset()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this video?</DialogTitle>
            <DialogDescription>
              &ldquo;{video.title}&rdquo; and its files, {formatBytes(video.size_bytes)}, are
              deleted from the archive. This cannot be undone. No poll downloads it again.
            </DialogDescription>
          </DialogHeader>
          {remove.isError && <ErrorBox error={remove.error} />}
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Delete video
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

export function VideoDetailPage() {
  const { id = "" } = useParams()
  const video = useQuery({ queryKey: queryKeys.video(id), queryFn: () => api.video(id) })

  return (
    <Page>
      <Link to="/videos" className="text-sm text-muted-foreground hover:underline">
        Back to videos
      </Link>
      <QueryState query={video}>
        {(v) => {
          const description = infoString(v.info, "description")
          return (
            <Card>
              <CardHeader>
                <CardTitle className="text-xl">{v.title}</CardTitle>
                <VideoYouTubeLink id={v.id} title={v.title} className="w-fit" />
                {(v.state === "complete" || v.state === "staged") && (
                  <CardAction>
                    <DeleteVideoButton video={v} />
                  </CardAction>
                )}
              </CardHeader>
              <CardContent className="flex flex-col gap-6">
                {v.state === "complete" && <VideoPlayer video={v} />}
                <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
                  <Field label="ID">
                    <span className="font-mono">{v.id}</span>
                  </Field>
                  <Field label="Channel">
                    <span className="inline-flex items-center gap-1.5">
                      {infoString(v.info, "channel") ?? v.channel_id}
                      <ChannelYouTubeLink id={v.channel_id} name={infoString(v.info, "channel")} />
                    </span>
                  </Field>
                  <Field label="Type">{v.type}</Field>
                  <Field label="State">
                    <Badge variant="outline">{v.state}</Badge>
                  </Field>
                  <Field label="Uploaded">{formatUploadDate(v.upload_date)}</Field>
                  <Field label="Duration">{formatDuration(v.duration)}</Field>
                  <Field label="Size on disk">{formatBytes(v.size_bytes)}</Field>
                  <Field label="Completed">
                    <Clock iso={v.completed_at} />
                  </Field>
                  <Field label="Verified">
                    {v.verified_at ? (
                      <>
                        <Clock iso={v.verified_at} /> (
                        {v.verified_checks?.join(", ") || "no check ran"})
                      </>
                    ) : (
                      "-"
                    )}
                  </Field>
                  <Field label="Views">{infoString(v.info, "view_count") ?? "-"}</Field>
                  <Field label="Resolution">{infoString(v.info, "resolution") ?? "-"}</Field>
                  <Field label="Format">{infoString(v.info, "format") ?? "-"}</Field>
                </dl>
                {description && (
                  <p className="max-h-48 overflow-auto text-sm whitespace-pre-wrap text-muted-foreground">
                    {description}
                  </p>
                )}
                {v.info && (
                  <details className="rounded-lg border">
                    <summary className="cursor-pointer p-3 text-sm font-medium">
                      Raw yt-dlp info
                    </summary>
                    <pre className="max-h-[32rem] overflow-auto border-t p-3 text-xs">
                      {JSON.stringify(v.info, null, 2)}
                    </pre>
                  </details>
                )}
              </CardContent>
            </Card>
          )
        }}
      </QueryState>
      {video.data?.state === "complete" && <FormatWatchCard videoId={id} />}
    </Page>
  )
}
