import os
from pathlib import Path

# Everything the app stores lives under one folder. Override it with OPEN_IMAGE_HOME.
HOME = Path(os.environ.get("OPEN_IMAGE_HOME") or Path.home() / "OpenImage").expanduser()
MODELS = HOME / "models"
DATA = HOME / "data"
WINDOW = DATA / "window"

# older versions kept pictures here; nothing writes to them any more
LEGACY_IMAGES = HOME / "images"
LEGACY_THUMBS = DATA / "thumbs"

for _d in (MODELS, DATA):
    _d.mkdir(parents=True, exist_ok=True)
