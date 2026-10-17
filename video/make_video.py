"""One command for the demo video (see video/README.md).

    uv run --with playwright --with imageio-ffmpeg python video/make_video.py --network base_sepolia
    uv run --with playwright --with imageio-ffmpeg python video/make_video.py --network anvil --record

``--record`` first captures a fresh local Anvil run (``pipeline/record.py``); without it the newest passed
capture is used. Every other option is passed to ``pipeline/build.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    argv = sys.argv[1:]
    if "--record" in argv:
        argv.remove("--record")
        rc = subprocess.call([sys.executable, str(HERE / "pipeline" / "record.py"), "--network", "anvil"])
        if rc != 0:
            print("make_video: the recording did not pass; the video was not built", file=sys.stderr)
            return rc
    return subprocess.call([sys.executable, str(HERE / "pipeline" / "build.py"), *argv])


if __name__ == "__main__":
    sys.exit(main())
