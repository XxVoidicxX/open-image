"""Limits for worker processes: GPU memory cap, low priority, a RAM watchdog and a pause when the GPU is hot."""
import os
import threading
import time

import psutil
import torch

VRAM_FRACTION = 0.80  # share of total VRAM the process may allocate
MIN_FREE_RAM_GB = 0.7
MIN_FREE_RAM_SECONDS = 6.0
STUCK_SECONDS = 40.0
MAX_PAGEFILE_GROWTH_GB = 1.5
THROTTLE_C = 80
COOLED_C = 74
ABORT_C = 88
EXIT_CODE_LOW_RAM = 42


class ThermalAbort(RuntimeError):
    pass


def gpu_temp():
    try:
        import pynvml
        pynvml.nvmlInit()
        return pynvml.nvmlDeviceGetTemperature(pynvml.nvmlDeviceGetHandleByIndex(0), pynvml.NVML_TEMPERATURE_GPU)
    except Exception:
        return None


def _watch_ram():
    # Loading a big model maps files into memory and free RAM can touch zero for a few seconds without any
    # real pressure. Exit only if the page file is growing too, or if RAM stays low for a long time.
    swap_start = psutil.swap_memory().used
    low_since = None
    while True:
        free = psutil.virtual_memory().available / 2**30
        if free < MIN_FREE_RAM_GB:
            low_since = low_since or time.time()
            low_for = time.time() - low_since
            paging = (psutil.swap_memory().used - swap_start) / 2**30 > MAX_PAGEFILE_GROWTH_GB
            if (paging and low_for >= MIN_FREE_RAM_SECONDS) or low_for >= STUCK_SECONDS:
                print(f"[guard] only {free:.2f} GiB of RAM left, exiting", flush=True)
                os._exit(EXIT_CODE_LOW_RAM)
        else:
            low_since = None
        time.sleep(0.5)


def step_callback(pipe, step, timestep, kwargs):
    temp = gpu_temp()
    if temp is not None:
        if temp >= ABORT_C:
            raise ThermalAbort(f"GPU reached {temp}C")
        if temp >= THROTTLE_C:
            while True:
                time.sleep(4)
                temp = gpu_temp()
                if temp is None or temp <= COOLED_C:
                    break
    return kwargs


def install():
    if torch.cuda.is_available():
        torch.cuda.set_per_process_memory_fraction(VRAM_FRACTION)
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass
    threading.Thread(target=_watch_ram, daemon=True).start()
    return step_callback
