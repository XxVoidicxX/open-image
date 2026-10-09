"""One process per loaded model. Reads JSON requests on stdin, writes JSON events on stdout.

Anything the libraries print goes to stderr; the real stdout is kept for events only.
"""
import base64
import io
import json
import os
import sys
import time
import traceback

_events = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
sys.stdout = sys.stderr


def pack(image):
    """The finished picture and a small preview, as base64 for the pipe. Nothing is written to disk."""
    full = io.BytesIO()
    image.save(full, "PNG")
    small = image.convert("RGB")
    small.thumbnail((560, 560))
    thumb = io.BytesIO()
    small.save(thumb, "JPEG", quality=84)
    return base64.b64encode(full.getvalue()).decode(), base64.b64encode(thumb.getvalue()).decode()


def emit(**event):
    _events.write(json.dumps(event) + "\n")
    _events.flush()


import torch  # noqa: E402

from . import engines, guard  # noqa: E402
from .catalog import BY_ID  # noqa: E402


def main():
    model = BY_ID[sys.argv[1]]
    step_guard = guard.install()
    engines.skip_allocator_warmup()
    started = time.time()
    engine = engines.create(model, emit)
    emit(type="ready", model=model.id, load_s=round(time.time() - started, 1))

    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        if req.get("cmd") == "quit":
            break
        job_id, steps = req["id"], req["steps"]

        def on_step(pipe, step, timestep, kwargs):
            emit(type="progress", id=job_id, step=step + 1, steps=steps)
            return step_guard(pipe, step, timestep, kwargs)

        try:
            began = time.time()
            image = engine.run(req, on_step)
            full, thumb = pack(image)
            del image
            emit(type="done", id=job_id, seconds=round(time.time() - began, 1), full=full, thumb=thumb)
        except guard.ThermalAbort as err:
            emit(type="error", id=job_id, kind="thermal", message=str(err))
        except torch.OutOfMemoryError:
            emit(type="error", id=job_id, kind="oom", message="The GPU ran out of memory at this size. Try a smaller shape or a lighter model.")
        except Exception as err:
            traceback.print_exc()
            emit(type="error", id=job_id, kind="error", message=f"{type(err).__name__}: {err}"[:300])
        finally:
            engine.release()
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()


if __name__ == "__main__":
    main()
