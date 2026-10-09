import statistics
import threading
import time

import psutil

from .paths import HOME

try:
    import pynvml
    pynvml.nvmlInit()
    _gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    pynvml = None
    _gpu = None

GIB = 2**30


def read():
    mem = psutil.virtual_memory()
    out = {"ram_total_gb": mem.total / GIB, "ram_avail_gb": mem.available / GIB, "gpu": None}
    try:
        out["disk_free_gb"] = psutil.disk_usage(str(HOME)).free / GIB
    except OSError:
        out["disk_free_gb"] = None
    if _gpu is not None:
        try:
            vram = pynvml.nvmlDeviceGetMemoryInfo(_gpu)
            out["gpu"] = {
                "name": str(pynvml.nvmlDeviceGetName(_gpu)),
                "temp_c": pynvml.nvmlDeviceGetTemperature(_gpu, pynvml.NVML_TEMPERATURE_GPU),
                "util": pynvml.nvmlDeviceGetUtilizationRates(_gpu).gpu,
                "vram_used_gb": vram.used / GIB,
                "vram_total_gb": vram.total / GIB,
            }
        except Exception:
            pass
    return out


class Monitor:
    """Tracks how busy the GPU is with other programs by sampling while we aren't generating."""

    def __init__(self):
        self.busy = lambda: False
        self.other_util = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        samples = []
        while True:
            time.sleep(2)
            if self.busy():
                continue
            gpu = read()["gpu"]
            if gpu:
                samples = (samples + [gpu["util"]])[-5:]
                self.other_util = statistics.median(samples)

    def slowdown(self, res, need_ram):
        """Expected slowdown against the recorded speed, with the reasons."""
        factor, notes = 1.0, []
        if self.other_util >= 15:
            factor *= 1 + 0.6 * min(self.other_util, 100) / 100
            notes.append(f"The GPU is {int(self.other_util)}% busy with other programs")
        gpu = res.get("gpu")
        if gpu:
            if gpu["temp_c"] >= 80:
                factor *= 1.4
                notes.append(f"The GPU is hot ({gpu['temp_c']}°C) and will throttle")
            elif gpu["temp_c"] >= 72:
                factor *= 1.1
                notes.append(f"The GPU is warm ({gpu['temp_c']}°C)")
        if res["ram_avail_gb"] < need_ram:
            factor *= 1.4
            notes.append(f"Only {res['ram_avail_gb']:.1f} GB of RAM is free (this model likes {need_ram:.0f} GB). Closing some apps will help")
        return factor, notes


monitor = Monitor()
