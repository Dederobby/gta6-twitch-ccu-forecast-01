"""gta6-twitch-ccu-forecast-01 entry: per-hour log-log OLS on the comparables.

Model
-----
For each forecast hour h = 2..24, fit across the case's comparable past
launches (each with a complete 24-hour release day) the regression

    ln(viewers_h) = a_h + b_h * ln(viewers_1) + noise

where viewers_1 is that launch's hour-1 print.  The forecast median is the
fitted value at the case's own hour-1 print, and the five quantiles are
exp(mu_h + z_tau * sigma_h) with z the standard-normal quantiles at
(0.05, 0.25, 0.5, 0.75, 0.95) and sigma_h the residual standard deviation
of the fit.  Nothing is tuned on the practice cases: the form was chosen
from leave-one-out validation on the comparables themselves.

Why this beats persistence: hour 1 (00:00-01:00 UTC) only holds the
regions that have already unlocked at local midnight (NZ/Australia/Asia/
Europe), while the US unlocks at 05:00Z/08:00Z, so later hours do not stay
at the hour-1 level.  The fitted slope b_h falls from ~1.05 at hour 2 to
~0.55 at hour 20: big hour-1 launches plateau, small hour-1 launches grow
several-fold later in the day.  A plain ratio-to-hour-1 median curve
ignores that scale effect and scores worse in leave-one-out.

Relevance filter: a comparable is used only when its hour 1 was a real
launch hour, i.e. its hour-1 print is at least 5% of its own release-day
maximum.  Launches with a single simultaneous global unlock later in the
UTC day (hour-1 print near zero, e.g. a 14:00Z unlock) have a different
shape regime -- their later/earlier ratios explode to 100x -- and
including them in training badly hurts (leave-one-out pinball 0.44 vs
0.26).  The Grand Theft Auto VI category's own pre-release history is
excluded automatically: it has no complete release day.

Fallbacks (an invalid case forfeits prizes, so the program never crashes):
if too few comparables pass the filter, relax it to every comparable with
a complete day; if still too few, fall back to the house persistence
baseline (median = hour-1 print, spread 0.15 * sqrt(h - 1) in log space).

Python 3.13 standard library only, no network, deterministic: the
training set is sorted by name, so two runs write identical bytes.
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so a module
# shipped beside this file imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
MIN_TRAIN = 8          # minimum comparables for the OLS fit
RELEVANCE = 0.05       # hour-1 print must be >= 5% of the launch's day max
BASELINE_SIGMA = 0.15  # fallback persistence spread (house baseline)


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up (same convention as the starter)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def parse_ts(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def day_curve(comp):
    """Release-day hourly viewers {hour 1..24: viewers}, or None if incomplete."""
    ds = parse_ts(comp.get("day_start"))
    if ds is None:
        return None
    curve = {}
    for row in comp.get("hourly") or []:
        at = parse_ts(row.get("from"))
        v = row.get("viewers")
        if at is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        h = int((at - ds).total_seconds() // 3600) + 1
        if 1 <= h <= 24:
            curve[h] = v
    if len(curve) != 24:
        return None
    return curve


def training_set(case):
    """Comparable launches usable for the regression, sorted by name."""
    curves = []
    for comp in case.get("comparables") or []:
        curve = day_curve(comp)
        if curve is None:
            continue
        curves.append((str(comp.get("name", "")), curve))
    curves.sort(key=lambda item: item[0])
    relevant = [c for _, c in curves if c[1] >= RELEVANCE * max(c.values())]
    if len(relevant) >= MIN_TRAIN:
        return relevant
    complete = [c for _, c in curves]
    if len(complete) >= MIN_TRAIN:
        return complete
    return relevant or complete


def ols(xs, ys):
    """Ordinary least squares y ~ a + b*x; returns (a, b)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return my - b * mx, b


def forecast_case(case):
    known = case.get("known") or []
    if isinstance(known, dict):
        known = [known]
    first = next((k for k in known if k.get("hour") == 1), None)
    if first is None and known:
        first = known[0]
    p1 = max(1.0, float(first["viewers"])) if first else 1.0
    hours = [row["hour"] for row in case.get("forecast") or []]
    if not hours:
        hours = list(range(2, 25))

    train = training_set(case)
    rows = []
    if len(train) >= 3:
        log_p1 = math.log(p1)
        for h in hours:
            xs = [math.log(c[1]) for c in train]
            ys = [math.log(c[h]) for c in train]
            a, b = ols(xs, ys)
            mu = a + b * log_p1
            resid = [y - (a + b * x) for x, y in zip(xs, ys)]
            mean_r = sum(resid) / len(resid)
            var = sum((r - mean_r) ** 2 for r in resid) / len(resid)
            sigma = max(0.08, math.sqrt(var))
            qs = [round2(min(1e9, max(1.0, math.exp(mu + z * sigma)))) for z in Z]
            # keep quantiles non-decreasing after rounding
            for i in range(1, 5):
                if qs[i] < qs[i - 1]:
                    qs[i] = qs[i - 1]
            rows.append({"hour": h, "q": qs})
    else:
        # persistence fallback (house baseline shape)
        for h in hours:
            spread = BASELINE_SIGMA * math.sqrt(h - 1)
            qs = [round2(min(1e9, max(1.0, p1 * math.exp(z * spread)))) for z in Z]
            for i in range(1, 5):
                if qs[i] < qs[i - 1]:
                    qs[i] = qs[i - 1]
            rows.append({"hour": h, "q": qs})
    return {"hours": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            forecast = forecast_case(case)
        except Exception:
            # Last-resort fallback: valid persistence output, never crash.
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
                known = case.get("known") or []
                if isinstance(known, dict):
                    known = [known]
                first = next((k for k in known if k.get("hour") == 1), known[0] if known else None)
                p1 = max(1.0, float(first["viewers"])) if first else 1.0
            except Exception:
                p1 = 1.0
            forecast = {"hours": [
                {"hour": h, "q": [round2(min(1e9, max(1.0, p1 * math.exp(z * BASELINE_SIGMA * math.sqrt(h - 1))))) for z in Z]}
                for h in range(2, 25)
            ]}
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
