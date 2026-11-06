"""
app/despike.py — drop single-sample sensor spikes from charts and metrics

A probe that opens, shorts, or catches electrical noise rails to a wild value
for one poll and snaps back on the next — e.g. a DHW mix sensor reading 250 F
(above water's boiling point) for one 5-minute read, then 120 F again
(90-94-100 Spruce St., 2026-11-05). One such point stretches a chart's Y-axis
and inflates every max / peak built from the series, so it is removed wherever
stored readings become a chart or a metric.

Only an ISOLATED point is removed: one that sits more than `jump` away from
BOTH of its nearest readings on either side, where those two readings agree
with each other. A real change (a boiler firing) is sustained — the next read
does not fall back — so its neighbours disagree and it is kept. Fast, genuinely
spiky signals (a stack temperature on a short burner cycle) are left to the
caller to exclude, since a lone high read there can be real.
"""

DEFAULT_JUMP = 40.0   # degrees F: a point this far from both agreeing neighbours is a glitch


def clean(vals, jump=DEFAULT_JUMP):
    """A copy of the time-ordered list `vals` (None marks a missing read) with
    each single-sample spike replaced by None. O(n)."""
    n = len(vals)
    out = list(vals)
    left = [None] * n          # nearest reading before i
    right = [None] * n         # nearest reading after i
    last = None
    for i in range(n):
        left[i] = last
        if vals[i] is not None:
            last = vals[i]
    last = None
    for i in range(n - 1, -1, -1):
        right[i] = last
        if vals[i] is not None:
            last = vals[i]
    for i in range(n):
        v = vals[i]
        if v is None:
            continue
        p, q = left[i], right[i]
        if p is None or q is None:          # an endpoint: not enough to judge by
            continue
        if abs(v - p) > jump and abs(v - q) > jump and abs(p - q) <= jump:
            out[i] = None
    return out


def drop(vals, jump=DEFAULT_JUMP):
    """The values with single-sample spikes removed entirely (for max / mean /
    peak). `vals` is time-ordered; None entries are ignored."""
    return [v for v in clean(vals, jump) if v is not None]


def fill(vals, jump=DEFAULT_JUMP):
    """Like clean, but each spike is replaced by the last good value before it
    (carry-forward) rather than None — so a corrected chart line has no gap and
    a stored series stays continuous. A genuinely missing read (None in the
    input) is left as None; only a removed spike is filled."""
    cleaned = clean(vals, jump)
    out = list(cleaned)
    last = None
    for i in range(len(out)):
        if out[i] is None and vals[i] is not None and last is not None:
            out[i] = last                 # a removed spike: carry the previous good value
        if out[i] is not None:
            last = out[i]
    return out
