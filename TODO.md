# TODO — next version

Planning notes for the next batch of changes. Current error handling works but is
far from ideal; this file scopes what "ideal" looks like and the open questions.

> Compat reminder (from CLAUDE.md): runtime is **Python 3.9+**. Anything below that
> relies on 3.11-only APIs (e.g. `Exception.add_note`) needs a fallback.

---

## Theme: error handling overhaul

### Current state (for reference)

- **Fail-hard is the default** (`SubFeed.raise_error = True`). On failure the exception
  is re-raised at `smartfeed/models/subfeed.py:47`, then propagates through the mixer's
  `asyncio.gather(...)` at `smartfeed/execution/executor.py:34` (no `return_exceptions`),
  so it bubbles out of `get_feed` raw.
- **Fail-soft is opt-in** (`raise_error=False`). The subfeed swallows the exception and
  returns `FeedResult(data=[], next_page={subfeed_id: <unadvanced cursor>}, has_next_page=False)`
  (`subfeed.py:47`). It is **completely silent** — no log, no debug stamp (empty data =
  nothing to stamp), so the caller cannot tell "source failed" from "source was empty".
- **Backfill of a dead source's slots** only happens via the wrapper refill loop
  (`wrapper.py:251` passthrough-with-dedup, `wrapper.py:663` cached cold build). A bare
  mixer or a non-dedup passthrough does **not** redistribute — the dead child's demand
  share just yields nothing and the page comes back short.
- The package currently has **no logging and no `print`** anywhere — there is no logger
  to hook into yet.

---

### 1. Fail-hard: propagate a clear, understandable error

**Problem.** When a hard failure crashes the whole feed, the exception that surfaces is
the raw error from the user's method. It carries no smartfeed-level context: *which*
subfeed (`subfeed_id`), *where* in the config tree, on *which* page/cursor. Debugging a
crash means guessing which source blew up.

**Desired.** The propagated error names the failing node and preserves the original
traceback — e.g. "SubFeed 'trending_posts' raised while fetching page N" chained to the
original exception, so `raise ... from e` keeps the root cause visible.

**Ideas / anchors.**
- Catch at `subfeed.py:47` in the `raise_error=True` branch; attach context, then re-raise.
- Preserve the original: either a dedicated `SubFeedError`/`SmartFeedError` that chains via
  `from e`, or annotate in place. **`Exception.add_note` is 3.11+** — needs a 3.9/3.10
  fallback (wrap in a custom exception, or prepend context to a re-raised copy).
- Consider the `gather` semantics: with the default, only the *first* exception is raised
  and sibling failures are discarded. Decide whether to keep first-wins or switch to
  `return_exceptions=True` and aggregate (e.g. surface an ExceptionGroup / a combined
  message) — `executor.py:34`.

**Open questions.**
- Introduce a real exception hierarchy (`SmartFeedError` base) or keep raising the
  original type with added context?
- Should the error object carry structured fields (node_id, cursor slice, page) for
  programmatic handling, not just a message?

---

### 2. Fail-soft: observability + self-heal without a dedup node

Two sub-goals.

**2a. Make soft failures observable (not silent).**

Today a soft failure vanishes. We want the failure recorded somewhere the integrator can
see. Options to weigh:
- Emit via the stdlib `logging` module (`logging.getLogger("smartfeed")`) — library-correct,
  but there's no logger anywhere yet, so this is a from-scratch decision on logger name /
  levels / what we log.
- Surface it in the result: a feed-level `errors` channel (per-source failure list) on
  `FeedResult`, or a debug entry. Note: item-level `_smartfeed_debug_info` won't work for a
  failed source because `data=[]` — there's no item to stamp. Needs a feed-level home.
- Optional user callback / hook (`on_source_error=...`) so the integrator decides.
- (Explicitly *not* `print` — a published library must not print.)

**Open questions.**
- Where does a soft error live: logs only, `FeedResult` field, callback, or several?
- Does adding an `errors` field to `FeedResult` break the documented "byte-exact" result
  shape? If so, gate it / version it.

**2b. Self-heal (backfill dead-source positions) even with no dedup node.** *(ideal / exploratory — no chosen design yet)*

Today, surviving siblings only backfill a dead source's slots when a dedup wrapper (or a
cached cold build) sits above, because only the wrapper runs a deficit-refill loop. Goal:
the page fills from healthy sources even when there's no dedup wrapper in the tree.

**Tensions / constraints to respect.**
- **Do not reintroduce over-fetch.** Over-fetching demand as a buffer was deliberately
  removed (commit `d799de8` "overfetch killed bc YAGNI"; pinned by
  `tests/test_bug_overfetch_item_loss.py`). Any backfill design must not silently discard
  fetched items or re-open that bug.
- The current mixer model fans out **once** via `gather` then `assemble` — there is no
  re-fetch round at the mixer level today.

**Candidate directions (pick / prototype later).**
- Generalize the wrapper's deficit-refill loop **down into mixers / the executor**, so any
  mixer can re-request the outstanding deficit from its surviving children (mirrors
  `wrapper.py:663`, without requiring dedup). Decide where it lives: `executor.run` for
  mixers, or a new `MixerNode` capability.
- Decide proportion behavior when a source dies: do the survivors' percentages
  **renormalize** to cover the gap, or just fill in configured order?
- Bound it like the wrapper does (`_REFILL_SCAN_FACTOR`) so a permanently-dead or
  duplicate-only source can't spin the loop forever.

**Open questions.**
- Should self-heal be always-on, or opt-in per mixer (a `backfill=True` flag)?
- Interaction with soft-fail's unadvanced self-scoped cursor (`subfeed.py:52`): a source
  that fails every round must not stall the loop — needs a "give up on this child" signal.

---

## Cross-cutting (don't forget when implementing)

- Add `tests/test_bug_*.py` pins for each fixed behavior (repo convention — don't fold into
  existing files).
- Update `README.md` "Error philosophy" + the `ARCHITECTURE.md` internals if behavior/keys
  move; add a `CHANGELOG.md` entry.
- Keep public annotations precise (`py.typed`); run `make lint` (ruff + pyright + black).
- Honor 3.9+ compat for any exception-notes / logging approach.
