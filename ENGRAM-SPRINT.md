# Engram — Qwen Hackathon Sprint (Track 1: MemoryAgent)

**Deadline:** Jul 9 2026, 4pm CDT · **Branch:** `feat/cognitive-memory-qwen`
**Judging:** Technical Depth 30% · Innovation 30% · Problem Value 25% · Presentation 15%
**The wedge:** the track rewards "timely *forgetting*" + "recall within limited context windows" —
the two things the vector-store crowd skips. That's Pillars 1 (two-tier) and 3 (metabolism).

**Week shape:** Mon–Wed = land the 3 pillars as working code. Thu–Fri = FTW QB path.
Weekend = Alibaba deploy proof + arch diagram + record 3-min video + Devpost submit.

---

## GATE 0 — tonight / before Monday (non-negotiable)
- [ ] Alibaba Cloud account live + credit coupon claimed
- [ ] Devpost entry created + Qwen Cloud Discord joined
- [ ] Qwen API key in `engram/.env` (DASHSCOPE/Qwen creds `qwen.py` expects)

> If this isn't done, Monday stalls. Everything below assumes a live Qwen account.

---

## MONDAY — Pillar 1 (two-tier) working + Qwen wired in
1. **Smoke-test `qwen.py` against the live account FIRST.** One embedding call + one
   chat call return 200. If this fails, stop and fix the account — don't build on a dead client.
2. **Slot Qwen embeddings into `intelligence.py:ask()`** behind the existing RRF surface:
   semantic rank = Qwen embeddings instead of TF-IDF. **Keep TF-IDF as the offline fallback** —
   that graceful degradation IS the "privacy-aware hybrid edge-cloud" story (capture local,
   cognition cloud, still works offline).
3. **Make two-tier promote/demote actually run** (`cognition.py`): working-set query returns
   within a token budget; a recall hit on a long-term memory promotes it to working.
- **EOD demo clip:** `engram ask "…"` returns Qwen-ranked, cited answer; working set respects the budget.

## TUESDAY — Pillar 2 (association) + start Pillar 3
1. **Persist the association graph** (`cognition.py` `memory_edges`): `connections()` already
   computes entity co-occurrence — write it as edges instead of recomputing.
2. **Recall spreads along edges** (spreading activation): a seed hit pulls its graph neighbors,
   not just keyword matches.
3. **Start metabolism skeleton:** consolidation pass that decays memory scores by age/access and
   logs to `consolidation_log`.
- **EOD demo clip:** `connections("FTW launch")` returns graph-spread results a keyword search misses.

## WEDNESDAY — Pillar 3 (metabolism = the money shot) + freeze
1. **The consolidation "sleep" loop** — the scoring differentiator. One pass that:
   - **decays** low-value memories,
   - **merges** duplicates (Qwen judges similarity),
   - **reconciles** contradictions (Qwen judges),
   - **reality-checks**: a memory naming a file/flag is verified it still exists (os.path / git)
     before it's trusted — stale ones demoted/flagged.
2. **Expose as `engram consolidate`** (one command, runnable on a schedule).
3. **Freeze the core.** No new features after Wednesday.
- **EOD demo clip:** run `consolidate` → watch a stale memory get flagged, a dup merged, a
  contradiction reconciled. **This is the centerpiece of the 3-min video.**

---

## THU–FRI — FTW QB path (separate lane, see Open Loops)

## WEEKEND (Jul 4–5) — submission polish (no new code)
- [ ] Deploy backend on **Alibaba Cloud** + record deployment proof (separate from demo)
- [ ] Architecture diagram (capture→OCR→privacy local | cognition→Qwen cloud — show the seam)
- [ ] Record ~3-min demo video (stitch the Mon/Tue/Wed clips + consolidate finale)
- [ ] Confirm MIT LICENSE present + repo public
- [ ] Devpost write-up + submit **before Jul 9, 4pm CDT**

## The one-line pitch (lead with it everywhere)
> Engram doesn't just store memories — it *forgets on purpose*. A local-first memory that
> captures privately on-device and reasons in the cloud on Qwen: two-tier recall under a token
> budget, association-graph spreading, and a metabolic sleep cycle that decays, merges,
> reconciles, and reality-checks what it knows.
