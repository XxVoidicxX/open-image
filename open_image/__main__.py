import argparse

from . import __version__


def main():
    parser = argparse.ArgumentParser(prog="open_image", description="Open Image: local image generation with a chat interface.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the app (default)")
    serve.add_argument("--no-window", action="store_true", help="run only the local server, with no window")

    dl = sub.add_parser("download", help="download models from Hugging Face")
    dl.add_argument("models", nargs="+", help="model ids, or 'all'")

    sub.add_parser("models", help="list the models and whether they are installed")

    args = parser.parse_args()
    if args.command == "download":
        from . import download
        download.run(args.models)
    elif args.command == "models":
        from .catalog import MODEL_LIST
        for m in MODEL_LIST:
            print(f"{'installed' if m.installed else '-':<10} {m.id:<18} {m.name}")
    else:
        from . import server
        server.serve(window=not getattr(args, "no_window", False))


if __name__ == "__main__":
    main()
