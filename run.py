#!/usr/bin/env python
"""파이프라인 진입점. 설치 없이 `python run.py <command>` 로 실행한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from shorts_factory.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
