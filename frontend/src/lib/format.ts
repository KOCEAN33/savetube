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

/** A stream's bitrate: "2.5 Mbps" or "130 kbps". */
export function formatKbps(kbps: number | null | undefined): string {
  if (kbps == null) return "-"
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${Math.round(kbps)} kbps`
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

/** Local time of day, with the date when it is not today: "02:00" or "10/23 02:00".
 * No year: pair it with formatDateTime in a tooltip where a row can be old. */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "-"
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, "0")
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.toDateString() === new Date().toDateString()
    ? time
    : `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${time}`
}

/** A recording's length in hours and minutes: "4:01" or "0:28". */
export function formatLength(seconds: number): string {
  const m = Math.floor(seconds / 60)
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`
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
