#!/usr/bin/env bash
# motion-mad.sh FILE...: print each file's motion score, the median over three
# 60 s windows at 25%, 50% and 75% of its duration of the mean absolute luma
# difference between consecutive frames, on frames scaled to 320x180 gray.
# docs/plan/encode-frame-rate.md, Motion score. Needs ffmpeg and ffprobe.
set -euo pipefail

window() { # file start -> mean frame MAD of 60 s from start
  ffmpeg -nostdin -v fatal -ss "$2" -t 60 -i "$1" -map 0:V:0 \
    -vf "scale=320:180,format=gray,tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-" \
    -f null - | awk -F= '/YAVG/ { s += $2; n++ } END { if (n) printf "%.3f\n", s / n }'
}

for f in "$@"; do
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  scores=()
  for p in 25 50 75; do
    scores+=("$(window "$f" "$(awk -v d="$dur" -v p="$p" 'BEGIN { printf "%.3f", d * p / 100 }')")")
  done
  median=$(printf '%s\n' "${scores[@]}" | sort -n | sed -n 2p)
  printf '%s\t%s\t%s\n' "$median" "${scores[*]}" "$f"
done
