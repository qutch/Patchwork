"""python -m matcher <recording.json> [-o candidates.json]   (prints candidates JSON)"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from matcher import match


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m matcher")
    p.add_argument("recording", type=Path)
    p.add_argument("-o", "--out", type=Path, help="also write candidates JSON here")
    args = p.parse_args(argv)
    recording = json.loads(args.recording.read_text())
    text = json.dumps(match(recording, base_dir=args.recording.parent), indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
