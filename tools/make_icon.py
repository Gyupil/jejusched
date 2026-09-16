"""`build/jejusched.ico`를 만든다 — 트레이와 같은 달력 그림을 쓴다.

    python tools/make_icon.py

`build/jejusched.spec`이 이 파일이 있으면 exe 아이콘으로 쓰고, 없으면 건너뛴다.
그림을 고치려면 `jejusched/ui/tray.py`의 `_icon_image()`를 고치고 다시 돌린다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#  윈도우가 상황(작업표시줄·바탕화면·알트탭)마다 다른 크기를 고른다
SIZES = (256, 128, 64, 48, 32, 16)
OUT = ROOT / "build" / "jejusched.ico"


def main() -> int:
    try:
        from jejusched.ui.tray import _icon_image
    except ImportError as exc:  # pragma: no cover
        print(f"Pillow가 필요하다 — `pip install -e '.[dev]'` ({exc})", file=sys.stderr)
        return 2

    #  각 크기를 **그 크기로 직접 그린다** — 큰 그림을 줄이면 16px에서 뭉갠다
    frames = [_icon_image(size) for size in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"{OUT} — {', '.join(f'{s}px' for s in SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
