"""Build OpenImage-<version>.exe into dist/.

Needs uv on PATH (it is bundled into the exe, and used here to make a throwaway build environment).
Run from the repository root:  python packaging/build.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
STAGE = BUILD / "stage"
DIST = REPO / "dist"


def version():
    return re.search(r'__version__ = "([^"]+)"', (REPO / "open_image" / "__init__.py").read_text()).group(1)


def main():
    ver = version()
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv was not found on PATH. Install it from https://docs.astral.sh/uv/ and try again.")
    shutil.rmtree(STAGE, ignore_errors=True)
    (STAGE / "app").mkdir(parents=True)
    shutil.copytree(REPO / "open_image", STAGE / "app" / "open_image", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "packaging" / "requirements.txt", STAGE / "requirements.txt")
    shutil.copy2(REPO / "open_image" / "static" / "icon.ico", STAGE / "icon.ico")
    shutil.copy2(uv, STAGE / "uv.exe")
    launcher = (REPO / "packaging" / "launcher.py").read_text(encoding="utf-8").replace('VERSION = "0.0.0"', f'VERSION = "{ver}"')
    (STAGE / "launcher.py").write_text(launcher, encoding="utf-8")

    env_dir = BUILD / "venv"
    python = env_dir / "Scripts" / "python.exe"
    if not python.exists():
        subprocess.run([uv, "venv", str(env_dir), "--python", "3.13"], check=True)
    subprocess.run([uv, "pip", "install", "--python", str(python), "pyinstaller==6.*"], check=True)

    name = f"OpenImage-{ver}"
    sep = ";"
    subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--name", name,
                    "--icon", str(STAGE / "icon.ico"),
                    "--add-data", f"{STAGE / 'app'}{sep}app",
                    "--add-data", f"{STAGE / 'requirements.txt'}{sep}.",
                    "--add-data", f"{STAGE / 'uv.exe'}{sep}.",
                    "--add-data", f"{STAGE / 'icon.ico'}{sep}.",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
                    str(STAGE / "launcher.py")], check=True)
    out = DIST / f"{name}.exe"
    print(f"built {out} ({out.stat().st_size / 2**20:.1f} MB)")


if __name__ == "__main__":
    main()
