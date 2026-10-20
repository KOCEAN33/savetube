import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { DEFAULT_FORMAT } from "@/lib/channel"

const QUALITIES: { format: string; label: string }[] = [
  { format: DEFAULT_FORMAT, label: "Best available" },
  { format: "bv*[height<=1080]+ba/b[height<=1080]", label: "Up to 1080p" },
  { format: "bv*[height<=720]+ba/b[height<=720]", label: "Up to 720p" },
  { format: "ba/b", label: "Audio only" },
]
const CUSTOM = "custom"

export function QualityField({
  id,
  format,
  onChange,
}: {
  id: string
  format: string
  onChange: (format: string) => void
}) {
  const preset = QUALITIES.some((q) => q.format === format)
  const [custom, setCustom] = useState(!preset)
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>Quality</Label>
      <Select
        value={custom ? CUSTOM : format}
        onValueChange={(v) => {
          setCustom(v === CUSTOM)
          if (v !== CUSTOM) onChange(v)
        }}
      >
        <SelectTrigger id={id} className="w-64">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {QUALITIES.map((q) => (
            <SelectItem key={q.format} value={q.format}>
              {q.label}
            </SelectItem>
          ))}
          <SelectItem value={CUSTOM}>Custom yt-dlp format</SelectItem>
        </SelectContent>
      </Select>
      {custom && (
        <Input
          aria-label="yt-dlp format"
          className="font-mono"
          required
          value={format}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      <p className="text-xs text-muted-foreground">
        yt-dlp format <code className="font-mono">{format || "-"}</code>. YouTube never serves the
        uploader's source file.
      </p>
    </div>
  )
}

export function DateCutoffField({
  id,
  value,
  onChange,
}: {
  id: string
  value: string | null
  onChange: (value: string | null) => void
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>Date cutoff</Label>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type="date"
          className="w-48"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value || null)}
        />
        {value && (
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange(null)}>
            Clear
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {value
          ? "Skip videos uploaded before this day. The check reads YouTube's approximate date, such as \"3 weeks ago\"."
          : "No cutoff: every video is kept."}
      </p>
    </div>
  )
}

export function CookiesField({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label className="font-normal">
        <Checkbox checked={checked} onCheckedChange={(on) => onChange(on === true)} />
        Use the account cookies
      </Label>
      <p className="pl-6 text-xs text-muted-foreground">
        Needed for members-only videos. Public videos download without them, which keeps the
        account out of reach of a ban.
      </p>
    </div>
  )
}
