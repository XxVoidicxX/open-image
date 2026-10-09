from huggingface_hub import snapshot_download

from .catalog import BY_ID, MARKER, MODEL_LIST


def fetch(model):
    print(f"{model.name}: downloading from {model.repo}", flush=True)
    model.path.mkdir(parents=True, exist_ok=True)
    (model.path / MARKER).unlink(missing_ok=True)
    snapshot_download(
        model.repo,
        local_dir=str(model.path),
        allow_patterns=list(model.allow) or None,
        ignore_patterns=list(model.ignore) or None,
        max_workers=8,
    )
    (model.path / MARKER).touch()
    print(f"{model.name}: done", flush=True)


def run(names):
    if names == ["all"]:
        targets = MODEL_LIST
    else:
        unknown = [n for n in names if n not in BY_ID]
        if unknown:
            raise SystemExit(f"unknown model: {', '.join(unknown)} (see `python -m open_image models`)")
        targets = [BY_ID[n] for n in names]
    for model in targets:
        fetch(model)
