---
status: active
---

# Show the 5-hour session window in the status line

## Context

llmeter renders the weekly cap but silently drops the 5-hour session window, even though Claude Code already delivers it on stdin every message and llmeter already parses, merges and persists it (`llmeter/adapters/claude_code.py:31`, `llmeter/core.py:290-326`). Only `format_line` at `llmeter/core.py:441-445` ignores it. So this is a rendering change plus one prerequisite bug fix, not a data-acquisition change.

Vendor sessions routed via `ANTHROPIC_BASE_URL` (Kimi, Qwen, etc) never receive `rate_limits` at all (verified live against 58 consecutive payloads), and llmeter's rendering is absence based, so non-Claude providers stay untouched by construction.

Exploration surfaced a real merge bug that would break a 5-hour meter: `_merge_caps` compares window identity only when both `resets_at` values are numeric (`core.py:313-325`). Two ISO strings fall through to max-percentage-wins, ratcheting the meter to its high-water mark forever; and in the mixed case the numeric side wins unconditionally, so an ISO-identified newer window loses to a numeric-identified older one. Harmless for a weekly window, fatal for one that resets every 5 hours. Fixed as part of this feature.

Repo note: this checkout tracks upstream `solarahorizon/llmeter` (not Raihaan's repo). Work happens on this local branch; when ready, `gh repo fork solarahorizon/llmeter --remote` adds a fork remote and the branch pushes there. Nothing in this spec depends on the fork existing yet.

## Goal

Show the rolling 5-hour session window (used % plus reset clock time) in the Claude Code status line alongside the existing weekly figure, with zero change to output for non-Claude providers.

## User journey

- Claude subscription session: `Fable 5 · ctx 30% (295k/1M) · 5h 22% (resets 14:30) · wk 37% (resets Tue 10:00)`. The 5h segment sits before wk, uses absolute local `%H:%M` (no weekday), and is always shown when the data exists (no config or env toggle).
- Fresh pane before the first API response: the payload lacks caps, so the existing cross-window snapshot fallback fills them; 5h rides along automatically.
- Vendor session: unchanged, e.g. `kimi-k3 · ctx 4% (40k/1M) · $0.06`. No 5h, no wk.
- Missing reset time: `5h 22% (resets ?)`, matching wk behaviour today.
- Hostile or malformed shapes (non-dict window, bool or string percentage): segment suppressed, never a crash; worst case the line falls back to the literal `llmeter`.

## Acceptance criteria

1. `python3 -m unittest discover -s tests` passes with zero failures or errors.
2. Formatting the standard test payload yields a line containing `5h 22% (resets HH:MM)` where HH:MM is the local `%H:%M` of epoch 1782050340, with no weekday in the 5h segment, placed after ctx and before wk.
3. The exact vendor line assertion at `tests/test_llmeter.py:116` (`kimi-k3 · ctx 4% (40k/1M) · $0.06`) passes byte for byte, and end-to-end vendor and gateway lines contain neither `wk` nor `5h`.
4. Merging two ISO-identified windows keeps the later one even at a lower percentage (no ratchet); an epoch and its equivalent ISO instant merge as the same window (max wins); a later ISO beats an earlier epoch.
5. A window with missing, junk or bool `resets_at` loses to any identified window regardless of percentage; two unidentified windows keep the max percentage.
6. `used_percentage: True/False` never renders and never wins a merge; the token guards at `core.py:426` and `:429` are untouched.
7. `fmt_reset(x)` output is identical to today for every previously valid input; `fmt_reset(x, "%H:%M")` renders local clock time; bool or junk input returns `?`.
8. Only four files change: `llmeter/core.py`, `tests/test_llmeter.py`, `README.md`, `docs/ROADMAP.md`. No new files, dependencies, or config/env toggles.

## Approach / how it works

All behaviour changes live in `llmeter/core.py`. No changes to `llmeter/statusline.py` or `llmeter/adapters/claude_code.py` (the adapter's `_WINDOWS` tuple already carries `five_hour`).

1. New `_reset_epoch(value)` helper next to `fmt_reset`: converts a `resets_at` (epoch seconds or ISO 8601 string) to float epoch, or `None` if unusable. Bool is JSON true/false, never a timestamp. Strict `fromisoformat` parity with today's parsing so previously valid inputs behave identically. Single source of truth for window identity (merge) and display.
2. `fmt_reset(value, fmt="%a %H:%M")`: parses via `_reset_epoch`, renders local time via `fromtimestamp(...).strftime(fmt)`. The default argument keeps every existing call site and rendered string unchanged. One deliberate delta: `fmt_reset(True)` previously rendered epoch 1 as a real time, now returns `?` (covered by a test).
3. New `_cap_pct(win)` guard: returns `used_percentage` from a window dict, or `None`, excluding bool. Used by both merge and render, closing the bool holes at `core.py:305-307` and `:443`.
4. Merge fix in `_merge_caps`: window identity comes from `_reset_epoch` on both sides instead of a numeric-type check. Later reset wins outright; equal reset (including an epoch vs its equivalent ISO) keeps the max percentage; an identified window beats an unidentified one regardless of percentage; two unidentified windows keep the max. All six existing MergeCapsTests pass unchanged by inspection. Docstring updated to say identity is parsed, not type gated.
5. Render: replace the single seven_day block at `core.py:441-445` with a small loop over `(("five_hour", "5h", "%H:%M"), ("seven_day", "wk", "%a %H:%M"))`, appending `"{label} {pct:.0f}% (resets {time})"` when `_cap_pct` is valid. Part order becomes model · ctx · 5h · wk · $. `format_line` docstring updated.

Kept semantics, stated explicitly: `format_line` prefers the message's own caps wholesale over the snapshot (`core.py:438-440`). A payload carrying only `five_hour` renders 5h without wk even when the snapshot knows wk. Per-window fallback would be scope creep and is not part of this change.

## Task breakdown

Task 1 - Core rendering and merge fix
- **Files**: `llmeter/core.py`
- **Consumes / produces**: consumes the existing Reading and caps shapes; produces `_reset_epoch`, `_cap_pct`, the `fmt` parameter on `fmt_reset`, the parsed-identity merge semantics, and the 5h render segment.
- **Parallel**: independent (sole owner of `core.py`).

Task 2 - Test coverage
- **Files**: `tests/test_llmeter.py`
- **Consumes / produces**: consumes Task 1's public behaviour; produces the regression test for the ratchet bug plus feature and hostile-shape coverage (detail in Testing below).
- **Parallel**: sequential (depends on Task 1; the test file is a single shared surface, one worker only).

Task 3 - Docs
- **Files**: `README.md`, `docs/ROADMAP.md`
- **Consumes / produces**: nothing from Tasks 1 or 2; produces the updated sample line and prose (`README.md:3`, `:6`, `:43`, `:182`) and drops "the 5-hour window" from the config-file opt-in roadmap item (`docs/ROADMAP.md:71-72`; `:39` is already accurate and stays).
- **Parallel**: independent (disjoint files).

## Out of scope

- Per-window snapshot fallback in `format_line` (message caps continue to win wholesale).
- Config file or env toggles; the roadmap item survives minus the 5-hour entry.
- Adapter changes: `_clean_caps` bool leniency stays, core is the defensive layer, and after the merge fix a bool percentage can never reach the merged snapshot.
- Relative countdown formatting (absolute clock time was chosen).
- New adapters, an adapter registry, or any other provider work.
- Fork, push and PR mechanics; the branch stays local until Raihaan forks upstream.

## Testing

Run with `python3 -m unittest discover -s tests`. Never execute the test file directly: a mid-file `__main__` guard at `tests/test_llmeter.py:351` silently skips the later classes.

Timezone rule for every new assertion: never hardcode a clock string; compute expectations in-test, e.g. `datetime.datetime.fromtimestamp(1782050340).strftime("%H:%M")`.

All test work is additive; no existing assertion needs changing. By class:

- **AdapterTests**: extend the payload-mapping test to assert `caps.five_hour.used_percentage == 22.0`.
- **HarvestTests**: extend the snapshot test to assert five_hour lands on disk.
- **RenderTests**: new test for `5h 22% (resets HH:MM)` before wk, asserting the whole segment by equality rather than a no-weekday regex (equality is the stronger check and needs no pattern); five_hour without seven_day and the inverse; missing `resets_at` renders `resets ?`; hostile shapes (`caps={"five_hour": 5}`, bool percentage, string percentage) suppress the segment without crashing; extend the cross-window fallback test with `5h 22%`; new `fmt_reset`/`_reset_epoch` cases (default vs `%H:%M`, ISO with offset matches its epoch, bool and junk give `?`/`None`, naive ISO, epoch passthrough).
- **MergeCapsTests** (use `five_hour` to mirror the live ratchet): ISO-identified newer window wins even at a lower percentage (the ratchet regression test); ISO same reset keeps max; epoch and equivalent ISO are the same window; later ISO beats earlier epoch; junk reset loses to an identified window and junk vs junk keeps max; bool percentage window is invalid; bool `resets_at` is unidentified. The six existing tests pass unchanged.
- **MainTests**: extend both end-to-end tests with `assertIn("5h 22%", line)`.
- **ProviderScopeTests**: strengthen the third-party and gateway isolation tests with `assertNotIn("5h", line)`.
- Byte-for-byte invariant: `tests/test_llmeter.py:116`, the exact vendor line.

Manual verification: pipe a captured payload through the real entry point, `PYTHONPATH=. python3 -m llmeter.statusline < sample.json`, and check `~/.claude/llmeter/usage-snapshot.json` already holds `caps.five_hour` from live sessions.

### Build notes (what actually landed)

- The bool guard turned out to be a live defect, not just a latent one: before `_cap_pct`, `{"seven_day": {"used_percentage": True}}` rendered `wk 1% (resets ?)`. The hostile-shape test covers both windows for that reason.
- `test_stale_republish_appends_no_history` leaked an open file handle and emitted a `ResourceWarning` once the suite grew. Switched to the file's existing `_lines` helper so the run stays pristine.
- The README needed more than the four listed lines: every place that described a weekly-only meter (why-it-exists, multi-window safety, the vendor section, requirements) now names both windows. Still the same four files overall.
- Verified on Python 3.9.6 and 3.14.0, 87 tests, and end to end through `llmeter.statusline` for the Claude, rollover and vendor cases.

## Custodian UI/UX checklist

- In a real Claude subscription session the status line shows the 5h segment before wk, and its percentage and reset time agree with the `/usage` panel.
- Reset time renders as local `HH:MM` with no weekday.
- In a vendor session (`ANTHROPIC_BASE_URL` set) the line is unchanged: spend shown, no 5h, no wk.
- A fresh pane before its first API response still shows 5h via the snapshot fallback.
- After a 5-hour rollover the meter drops to the new window's percentage instead of sticking at the old high-water mark.
- The longer line still fits comfortably in a normal-width terminal.
