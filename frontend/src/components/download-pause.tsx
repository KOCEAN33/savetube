import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PauseIcon, PlayIcon } from "lucide-react"

import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { api, mutations } from "@/lib/api"

// Pauses or resumes every archive download. Pausing asks first, because it
// stops the downloads that are running.
export function DownloadPauseToggle() {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const settings = useQuery({ queryKey: ["download-settings"], queryFn: api.downloadSettings })
  const toggle = useMutation({
    mutationFn: mutations.setDownloadsPaused,
    onSuccess: (updated) => {
      queryClient.setQueryData(["download-settings"], updated)
      setConfirming(false)
    },
  })

  if (!settings.data) return null
  if (settings.data.paused)
    return (
      <div className="flex items-center gap-2">
        <Badge variant="destructive">
          <PauseIcon />
          Downloads paused
        </Badge>
        <Button
          size="sm"
          variant="outline"
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(false)}
        >
          <PlayIcon />
          Resume
        </Button>
      </div>
    )
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
        <PauseIcon />
        Pause downloads
      </Button>
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Pause all archive downloads?</DialogTitle>
            <DialogDescription>
              No new download starts. Running downloads stop within about 10 seconds and keep
              their partial files, so they continue where they stopped after you resume. Live
              recordings keep running.
            </DialogDescription>
          </DialogHeader>
          {toggle.isError && <ErrorBox error={toggle.error} />}
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={toggle.isPending}
              onClick={() => toggle.mutate(true)}
            >
              Pause downloads
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
