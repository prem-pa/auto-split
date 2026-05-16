# Idea log

Future product ideas, jotted as they come up. Newest first.
Format per entry: short title, date, what + why, optional notes.

---

## Deploy on Raspberry Pi Zero 2 W
**2026-05-16**

Move the bot off the dev laptop onto an existing Pi Zero 2 W
(quad-core A53, **512MB RAM**, WiFi only, microSD).

**Open questions**
- Real memory footprint under load. Rough estimate was 350-450MB
  resident but that's a guess based on the dep list, not measurement.
  **Decision-blocker:** measure actual RSS of `uvicorn app.main:app`
  (no `--reload`, single worker) at idle and during a real receipt
  capture before committing to the Pi. Use the one-liner from chat:
    ```bash
    PID=$(pgrep -fn "uvicorn app.main")
    while sleep 1; do ps -o rss= -p "$PID" | awk '{printf "%6.1f MB\n", $1/1024}'; done
    ```
- macOS RSS vs ARM Linux RSS isn't 1:1. Mac measurement is a floor;
  the Pi could be 20-30% higher.
- Python 3.12 (Pi OS default) vs 3.14 (dev laptop) differ by ~5-10MB
  at baseline.

**Sketch (if feasible)**
- Pi OS Lite 64-bit, Python 3.12, `uv` for env management.
- `uvicorn app.main:app --workers 1` as a systemd service.
- Named `cloudflared` tunnel as systemd service for a stable URL.
- Systemd memory limits: `MemoryHigh=350M`, `MemoryMax=450M`,
  `Restart=on-failure`. OOM kill → auto-restart.
- 1GB swap file (not zram — Python's resident set can't be compressed
  meaningfully). High-endurance microSD (Samsung PRO Endurance or
  SanDisk Max Endurance).
- Langfuse stays on cloud — the Pi can't run a ClickHouse stack.

**Fallback paths**
- Pi 4 4GB (~$45) if Zero 2 W OOMs too often. Same software, way more headroom.
- Oracle Always Free as the cloud option (real reclaim risk).

---

## Receipt deduplication
**2026-05-16**

If a user tries to record the same receipt twice, the bot should notice
and ask if they really mean to add it again — rather than silently
creating a duplicate on Splitwise.

**Sketch**
- On photo capture, compute a hash of the image. Use a **perceptual hash**
  (e.g. pHash) so re-photographing the same bill at a slightly different
  angle / crop still matches.
- Persist the hash with the completed expense (new column on
  `expenses_completed`).
- On the next capture, look up the new hash against recent completed
  expenses. If a match exists within a TTL window (~60 days?), the
  confirmation reads:
  > *"Looks like you already added this on 2026-05-12 ($47.32, split with
  > Shreya). Add it again?"*
  with Yes / Cancel buttons.
- Yes → proceed as normal. Cancel → drop the pending row.

**Open questions**
- Match scope: per-user, per-group, or global? Per-group is most useful for
  catching "Hardik and Shreya both tried to capture last night's dinner."
- pHash threshold tuning — too lax matches different receipts at the same
  merchant; too strict misses real dupes (rotation, glare, partial reshot).
- Hash before or after parsing? Hashing first saves a Gemini call on
  obvious dupes, but you still need to display the matched expense's
  details, which means a DB lookup either way.
