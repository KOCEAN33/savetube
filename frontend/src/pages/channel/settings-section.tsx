import { useState, type FormEvent } from "react"
import { useMutation } from "@tanstack/react-query"

import { DateCutoffField, QualityField } from "@/components/channel-fields"
import { ErrorBox } from "@/components/query-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { mutations, type Channel, type ChannelSettings, type Membership } from "@/lib/api"
import { PLAYLISTS_PASS_HINT } from "@/lib/channel"
import { formatDateTime } from "@/lib/format"
import { useInvalidate } from "@/pages/channel/use-invalidate"

// Read-only: polls check each cookie profile on their own.
function MembershipList({ memberships }: { memberships: Membership[] }) {
  return (
    <div className="flex flex-col gap-2">
      <Label>Members-only access</Label>
      <p className="text-xs text-muted-foreground">
        A members-only video downloads with a cookie profile whose membership is confirmed. A poll
        that lists a members-only video checks each profile, at most once a day.
      </p>
      {memberships.length === 0 ? (
        <p className="text-xs text-muted-foreground">No profile checked yet.</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {memberships.map((m) => (
            <li key={m.profile} className="flex items-center gap-2">
              <span className="font-mono">{m.profile}</span>
              <Badge variant={m.is_member ? "default" : "secondary"}>
                {m.is_member ? "member" : "not a member"}
              </Badge>
              <span className="text-xs text-muted-foreground">
                checked {formatDateTime(m.checked_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function SettingsSection({ channel }: { channel: Channel }) {
  const invalidate = useInvalidate()
  const [form, setForm] = useState<
    Omit<ChannelSettings, "watch_lives"> & Pick<Channel, "members_all" | "playlists_pass">
  >({
    date_after: channel.date_after,
    format: channel.format,
    members_all: channel.members_all,
    playlists_pass: channel.playlists_pass,
  })
  const save = useMutation({
    mutationFn: () => mutations.updateChannel(channel.id, form),
    onSuccess: invalidate,
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate()
  }

  return (
    <form onSubmit={submit} className="flex max-w-xl flex-col gap-6 p-4">
      <DateCutoffField
        id="channel-date-after"
        value={form.date_after}
        onChange={(date_after) => setForm({ ...form, date_after })}
      />
      <QualityField
        id="channel-format"
        format={form.format}
        onChange={(format) => setForm({ ...form, format })}
      />
      <div className="flex flex-col gap-2">
        <Label className="font-normal">
          <Checkbox
            checked={form.members_all}
            onCheckedChange={(on) => setForm({ ...form, members_all: on === true })}
          />
          Download every members-only video
        </Label>
        <p className="text-xs text-muted-foreground">
          Title words do not apply to members-only videos. They download only while a cookie
          profile is a confirmed member of this channel.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <Label className="font-normal">
          <Checkbox
            checked={form.playlists_pass}
            onCheckedChange={(on) => setForm({ ...form, playlists_pass: on === true })}
          />
          Keep every video in the chosen playlists
        </Label>
        <p className="text-xs text-muted-foreground">{PLAYLISTS_PASS_HINT}</p>
      </div>
      <MembershipList memberships={channel.memberships} />
      {save.isError && <ErrorBox error={save.error} />}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending || !form.format.trim()}>
          Save
        </Button>
        {save.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
      </div>
    </form>
  )
}
