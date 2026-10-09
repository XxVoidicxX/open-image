import json
import statistics
import threading

from . import resources
from .catalog import QUALITIES, size_for
from .paths import DATA

_FILE = DATA / "timings.json"
_lock = threading.Lock()


def _read():
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def unit_cost(model):
    """Seconds per megapixel per step. Recent real runs win over the catalog default."""
    runs = _read().get(model.id, {}).get("units")
    return statistics.median(runs[-5:]) if runs else model.unit


def load_cost(model):
    runs = _read().get(model.id, {}).get("loads")
    return statistics.median(runs[-5:]) if runs else model.load_s


def record(model, unit=None, load=None):
    with _lock:
        data = _read()
        entry = data.setdefault(model.id, {"units": [], "loads": []})
        if unit:
            entry["units"] = (entry["units"] + [unit])[-10:]
        if load:
            entry["loads"] = (entry["loads"] + [load])[-10:]
        _FILE.write_text(json.dumps(data), encoding="utf-8")


def estimate(model, aspect, quality, loaded_id, res=None):
    res = res or resources.read()
    width, height = size_for(model, aspect)
    steps = model.steps[QUALITIES.index(quality)]
    factor, notes = resources.monitor.slowdown(res, model.need_ram)
    gen = unit_cost(model) * (width * height / 1e6) * steps * factor + 2
    load = 0.0 if loaded_id == model.id else load_cost(model) * (1.3 if factor >= 1.3 else 1.0)
    overhead = model.overhead_s * factor
    return {"gen_s": gen, "load_s": load, "overhead_s": overhead, "total_s": gen + load + overhead,
            "width": width, "height": height, "steps": steps, "notes": notes}
