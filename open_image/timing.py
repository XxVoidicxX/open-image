"""Wait-time estimates that learn from this machine.

Every finished image records how long it took together with the conditions at the time (how busy the GPU was with
other programs, free RAM, GPU temperature). To estimate, runs that happened under similar conditions count the most,
newer runs count more than older ones, and the catalog defaults only fill the gaps while there is little data.
"""
import json
import math
import statistics
import threading
import time

from . import resources
from .catalog import QUALITIES, size_for
from .paths import DATA

_FILE = DATA / "timings.json"
_lock = threading.Lock()
RUNS_KEPT = 40
NEUTRAL = {"util": 0, "ram": 8.0, "temp": 55}


_cache = (None, {})


def _read():
    global _cache
    try:
        stamp = _FILE.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache[0] == stamp:
        return _cache[1]
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for key, entry in list(data.items()):
        if "runs" not in entry:  # older format: plain lists of numbers
            runs = [{"t": 0, "unit": u, "ctx": NEUTRAL} for u in entry.get("units", [])]
            runs += [{"t": 0, "load": v, "ctx": NEUTRAL} for v in entry.get("loads", [])]
            data[key] = {"runs": runs}
    _cache = (stamp, data)
    return data


def context(res=None):
    res = res or resources.read()
    gpu = res.get("gpu") or {}
    return {"util": round(resources.monitor.other_util), "ram": round(res["ram_avail_gb"], 1), "temp": gpu.get("temp_c", NEUTRAL["temp"])}


def record(model, unit=None, load=None, prep=None, ctx=None):
    with _lock:
        data = _read()
        runs = data.setdefault(model.id, {"runs": []})["runs"]
        runs.append({"t": time.time(), "unit": unit, "load": load, "prep": prep, "ctx": ctx or context()})
        data[model.id]["runs"] = runs[-RUNS_KEPT:]
        _FILE.write_text(json.dumps(data), encoding="utf-8")


def _similarity(now, then, need_ram):
    gap = abs(now["util"] - then["util"]) / 25 + abs(now["temp"] - then["temp"]) / 12
    if (now["ram"] < need_ram) != (then["ram"] < need_ram):
        gap += 1
    return math.exp(-gap)


def _learned(model, key, ctx, default, factor=1.0):
    """Returns (value, number of runs that count for this reading, total runs of this kind)."""
    rows = [r for r in _read().get(model.id, {}).get("runs", []) if r.get(key) is not None]
    if not rows:
        return default * factor, 0.0, 0
    count = len(rows)
    weights = [0.92 ** (count - 1 - i) * _similarity(ctx, r["ctx"], model.need_ram) for i, r in enumerate(rows)]
    support = sum(weights)
    similar = sum(w * r[key] for w, r in zip(weights, rows)) / support if support else None
    quiet = [r[key] for r in rows if r["ctx"]["util"] < 15] or [r[key] for r in rows]
    fallback = statistics.median(quiet) * factor
    trust = min(1.0, support / 3.0)
    value = trust * similar + (1 - trust) * fallback if similar is not None else fallback
    return value, support, count


def unit_cost(model, ctx, factor):
    return _learned(model, "unit", ctx, model.unit, factor)[0]


def load_cost(model, ctx):
    return _learned(model, "load", ctx, model.load_s)[0]


def prep_cost(model, ctx, factor):
    if not model.overhead_s:
        return 0.0
    return _learned(model, "prep", ctx, model.overhead_s, factor)[0]


def estimate(model, aspect, quality, loaded_id, res=None):
    res = res or resources.read()
    ctx = context(res)
    width, height = size_for(model, aspect)
    steps = model.steps[QUALITIES.index(quality)]
    factor, notes = resources.monitor.slowdown(res, model.need_ram)
    unit, support, runs = _learned(model, "unit", ctx, model.unit, factor)
    gen = unit * (width * height / 1e6) * steps + 2
    load = 0.0 if loaded_id == model.id or model.overhead_s else load_cost(model, ctx)
    prep = prep_cost(model, ctx, factor)
    if runs >= 3 and support >= 1.5:
        basis = f"Learned from {runs} images made on this PC"
    elif runs:
        basis = f"Based on {runs} image{'s' if runs > 1 else ''} so far. It gets sharper as you generate"
    else:
        basis = "Starting estimate. It gets sharper as you generate"
    return {"gen_s": gen, "load_s": load, "overhead_s": prep, "total_s": gen + load + prep, "width": width, "height": height,
            "steps": steps, "notes": notes, "basis": basis, "runs": runs}
