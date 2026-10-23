const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "-"
  let value = bytes
  let unit = 0
  while (Math.abs(value) >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1
  return `${value.toFixed(digits)} ${BYTE_UNITS[unit]}`
}

/** Clock form for a single video: 1:02:03 or 4:05. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "-"
  const s = Math.round(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, "0")
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`
}

/** Totals across many videos: "1,234.5 h". */
export function formatHours(seconds: number): string {
  return `${(seconds / 3600).toLocaleString("en-US", { maximumFractionDigits: 1 })} h`
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-US")
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-"
  return new Date(iso).toLocaleString()
}

/** A time in Korea, where the streams the user watches are announced: "10/24/2026, 21:00 KST". */
export function formatKst(iso: string): string {
  const text = new Date(iso).toLocaleString(undefined, {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  })
  return `${text} KST`
}

/** Local time of day, with the date when it is not today: "02:00" or "10/23/2026, 02:00". */
export function formatClock(iso: string): string {
  const d = new Date(iso)
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  return d.toDateString() === new Date().toDateString()
    ? time
    : `${d.toLocaleDateString()}, ${time}`
}

/** Local calendar day `daysAgo` days before today, as YYYY-MM-DD. */
export function isoDay(daysAgo = 0): string {
  const d = new Date()
  d.setDate(d.getDate() - daysAgo)
  const month = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${d.getFullYear()}-${month}-${day}`
}

/** yt-dlp upload_date is YYYYMMDD. ISO dates pass through. */
export function formatUploadDate(value: string | null | undefined): string {
  if (!value) return "-"
  return /^\d{8}$/.test(value)
    ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`
    : value
}
