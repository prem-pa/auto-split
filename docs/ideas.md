# Idea log

Future product ideas, jotted as they come up. Newest first.
Format per entry: short title, date, what + why, optional notes.

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
