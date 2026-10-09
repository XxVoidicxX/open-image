import os
from pathlib import Path

# Everything the app stores lives under one folder. Override it with OPEN_IMAGE_HOME.
HOME = Path(os.environ.get("OPEN_IMAGE_HOME") or Path.home() / "OpenImage").expanduser()
MODELS = HOME / "models"
IMAGES = HOME / "images"
DATA = HOME / "data"
THUMBS = DATA / "thumbs"

for _d in (MODELS, IMAGES, DATA, THUMBS):
    _d.mkdir(parents=True, exist_ok=True)
