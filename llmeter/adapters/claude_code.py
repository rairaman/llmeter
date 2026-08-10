"""Adapter #1 — Claude Code.

Claude Code is the one tool that *pushes* its usage signal: it spawns the
configured ``statusLine`` command on every message and pipes a JSON payload
to it on stdin. For Pro/Max subscribers that payload carries ``rate_limits``
— the same five-hour / seven-day ``used_percentage`` + ``resets_at`` the
in-CLI /usage panel shows — which appears nowhere else on disk. This adapter
maps that payload to a normalized Reading.

Payload shape (Anthropic's, and theirs to change — read defensively)::

    { "session_id": "…",
      "model": {"id": "…", "display_name": "…"},
      "context_window": {"used_percentage": 30,
                         "total_input_tokens": 295000,
                         "context_window_size": 1000000},
      "rate_limits": {"five_hour": {"used_percentage", "resets_at"},
                      "seven_day": {"used_percentage", "resets_at"}} }
"""

import os

from .. import core

SOURCE = "claude-code"

# We persist ONLY these windows + fields — an allowlist, not the raw
# rate_limits dict. If Claude Code ever nests account/plan/user metadata under
# rate_limits, it must never silently land in ~/.claude/llmeter/ (codex P2:
# that would break the "only local usage numbers are saved" privacy promise).
_WINDOWS = ("five_hour", "seven_day")
_FIELDS = ("used_percentage", "resets_at")


def _clean_caps(rl):
    """Extract only the known windows + fields from rate_limits."""
    if not isinstance(rl, dict):
        return {}
    out = {}
    for window in _WINDOWS:
        w = rl.get(window)
        if not isinstance(w, dict):
            continue
        entry = {}
        for field in _FIELDS:
            if field in w and isinstance(w[field], (int, float, str)):
                entry[field] = w[field]
        if entry:
            out[window] = entry
    return out


def _ctx_window_int(v):
    """Parse a context-window token count; None on anything unusable."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _context_window_overrides():
    """model id -> true context-window tokens, for custom models Claude Code
    mis-sizes (it falls back to 200k for any model outside its own table).
    Built-in defaults are merged under the LLMETER_CONTEXT_WINDOWS env var
    ("id1=tokens1,id2=tokens2"); malformed entries are silently ignored so a
    bad env value can never break the status line."""
    overrides = {
        # Custom models routed through a proxy: Claude Code reports 200k, the
        # real window is larger. Add yours here, or set LLMETER_CONTEXT_WINDOWS.
        "qwen3.8-max": 1_000_000,
        # Retired by Alibaba 2026-08-05; kept so replayed old sessions still size right.
        "qwen3.8-max-preview": 1_000_000,
        # Moonshot Kimi. Sizes per platform.kimi.ai/docs/guide/claude-code-kimi
        # (checked 2026-08-10). The "[1m]" spelling is the model id used on the
        # pay-as-you-go endpoint; the subscription endpoint takes the bare id.
        "kimi-k3": 1_048_576,
        "kimi-k3[1m]": 1_048_576,
        "kimi-k2.7-code": 262_144,
        "kimi-k2.7-code-highspeed": 262_144,
    }
    for chunk in os.environ.get("LLMETER_CONTEXT_WINDOWS", "").split(","):
        if "=" not in chunk:
            continue
        mid, _, val = chunk.partition("=")
        n = _ctx_window_int(val.strip())
        if mid.strip() and n:
            overrides[mid.strip()] = n
    return overrides


def _session_spend(data):
    """Session cost, but only for a session routed to a third-party endpoint.

    Such a session can never show ``wk``. Claude Code fetches plan utilization
    only for the Anthropic subscription (``GET /api/oauth/usage``, an OAuth
    call that ignores ``ANTHROPIC_BASE_URL``) and forwards no cap of any kind
    in the status-line payload. Measured 2026-08-10 on Claude Code 2.1.226: 57
    consecutive payloads from a live Kimi session carried no ``rate_limits``
    key at all; the vendor returned no rate-limit response headers; and no
    usage endpoint answered on that vendor's API. So the number is not being
    dropped by llmeter, it never arrives.

    ``cost.total_cost_usd`` does arrive, and for a metered vendor session it is
    the usage signal that matters. Rendered as ``$N.NN`` in the slot ``wk``
    cannot fill (see ``core.format_line``).

    Left as None on the default provider, where spend is not the meaningful
    number on a subscription and ``wk`` is the real signal. Returns a value for
    0.0 as well, so a fresh window shows an honest ``$0.00`` rather than
    falling back to the previous session's total.
    """
    try:
        if core.provider_key() == core.DEFAULT_PROVIDER:
            return None
    except Exception:  # never break the status line over provider detection
        return None
    spend = core.dget(data, "cost").get("total_cost_usd")
    if isinstance(spend, bool) or not isinstance(spend, (int, float)):
        return None
    if spend < 0:
        return None
    return {"session_usd": spend}


def parse(data):
    """Claude Code statusLine payload -> normalized Reading (see core).

    Always returns a Reading (so the live line can show model + context even
    before any cap is known); ``caps`` is {} until the session's first API
    response populates ``rate_limits``. Never raises on a surprising shape,
    and only ever carries the allowlisted usage fields (see _clean_caps).
    """
    if not isinstance(data, dict):
        data = {}
    model = core.dget(data, "model")
    cw = core.dget(data, "context_window")
    reading = {
        "source": SOURCE,
        "model": model.get("display_name") or model.get("id"),
        "context_pct": cw.get("used_percentage"),
        # total_input_tokens is the exact sum used_percentage is computed from
        # (input + cache_creation + cache_read); context_window_size is the
        # model's max (200k, or 1M for extended-context models).
        "context_tokens": cw.get("total_input_tokens"),
        "context_window_size": cw.get("context_window_size"),
        "caps": _clean_caps(data.get("rate_limits")),
        "cost": _session_spend(data),
        "session_id": data.get("session_id"),
    }
    # Custom-model context-window correction. Claude Code only knows the
    # window of models in its own table; for everything else (e.g. a custom
    # qwen routed through a proxy) it reports 200k. Substitute the real window
    # and recompute the percentage from the absolute token count so the line
    # (ctx% and tokens/window) stays internally consistent. Built-in defaults
    # cover known custom models; extend per-user via LLMETER_CONTEXT_WINDOWS.
    _ovs = _context_window_overrides()
    _ov = _ovs.get(model.get("id")) or _ovs.get(model.get("display_name"))
    if _ov:
        _old = reading["context_window_size"]
        _toks = reading["context_tokens"]
        if isinstance(_toks, (int, float)) and not isinstance(_toks, bool) and _toks > 0:
            reading["context_pct"] = min(100.0, _toks * 100.0 / _ov)
        elif isinstance(reading["context_pct"], (int, float)) and isinstance(_old, (int, float)) and _old > 0:
            reading["context_pct"] = reading["context_pct"] * _old / _ov
        reading["context_window_size"] = _ov
    return reading
