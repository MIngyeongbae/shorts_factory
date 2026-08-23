"""끝 프레임 OCR 게이트 — `[7. videogen]` 검수 ①. ADR-0056 결정 6, 스펙 05 `[7]`.

스펙은 규칙만 둔다: **"라벨이 전부 읽히고 계약에 없는 숫자가 없다"** (`info` 씬),
**"글자가 없어야 통과"** (`info` 없는 씬). 구현체는 코드가 고른다 — 여기서는 `tesseract`
CLI가 PATH에 있으면 쓰고, 없으면 `[7]`이 **OCR 게이트만 건너뛰고 경고를 기록한다**
(D-3의 태도 — 비전 검수는 그대로 돈다).

라벨은 ASCII다 (ADR-0056 결정 3). 라틴 문자·숫자는 OCR이 신뢰되고 한글은 그렇지 않았다
— 그래서 이 게이트가 기계이고 옛 `[6i]`는 비전 자소 대조였다 (스펙 03 「화면 텍스트」).

## 대조 규칙

- 정규화: 소문자, 공백 제거. `"4 mm"`와 `"4mm"`는 같다 (OCR이 띄어쓰기를 흔든다)
- `info` 씬: 계약의 라벨 문자열이 **각각** 정규화 텍스트에 부분 문자열로 있어야 한다.
  숫자가 든 토큰 중 어느 라벨에도 속하지 않는 것이 있으면 **계약에 없는 숫자**다
- `info` 없는 씬: 영숫자 2자 이상의 토큰이 하나라도 있으면 글자가 있는 것이다. 한 글자
  토큰은 무시한다 — 질감 위에서 tesseract가 내는 잡음(`|`, `l`)을 글자로 세면 전 씬이
  사다리를 타고 내려간다
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

DEFAULT_TESSERACT = "tesseract"
TESSERACT_TIMEOUT = 60


class OCRError(Exception):
    """OCR 실행 실패 — 게이트 판정이 아니라 도구 문제다."""


class OCR(Protocol):
    name: str

    def read(self, image: Path) -> str:
        """이미지 → 읽힌 텍스트 (없으면 빈 문자열)."""
        ...


class TesseractOCR:
    """`tesseract {image} stdout` — 설치돼 있을 때만 쓴다 (`detect_ocr`)."""

    name = "tesseract"

    def __init__(self, executable: str = DEFAULT_TESSERACT, runner: Callable[..., Any] = subprocess.run) -> None:
        self.executable = executable
        self.runner = runner

    def read(self, image: Path) -> str:
        cmd = [self.executable, str(image), "stdout", "--psm", "11"]
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=TESSERACT_TIMEOUT, check=False)
        except FileNotFoundError as exc:
            raise OCRError(f"tesseract를 찾을 수 없다: {self.executable!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OCRError(f"tesseract가 {TESSERACT_TIMEOUT}초 안에 끝나지 않았다") from exc
        if getattr(proc, "returncode", 1) != 0:
            raise OCRError(f"tesseract 실패 (exit={proc.returncode}): {(proc.stderr or '')[:300]}")
        return proc.stdout or ""


class FakeOCR:
    """테스트용. 파일 줄기 이름(`3-1-end`)에 **정확히** 맞는 텍스트를 돌려준다."""

    name = "fake"

    def __init__(self, texts: dict[str, str] | None = None, *, default: str = "") -> None:
        self.texts = dict(texts or {})
        self.default = default
        self.calls: list[Path] = []

    def read(self, image: Path) -> str:
        self.calls.append(Path(image))
        return self.texts.get(Path(image).stem, self.default)


def detect_ocr(executable: str = DEFAULT_TESSERACT) -> OCR | None:
    """PATH에 tesseract가 있으면 어댑터, 없으면 `None` — 부재는 경고이지 실패가 아니다."""
    if shutil.which(executable):
        return TesseractOCR(executable)
    return None


# --- 대조 ----------------------------------------------------------------------

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%/x-]*")


def normalize(text: str) -> str:
    return _WS.sub("", text).lower()


@dataclass(frozen=True)
class OcrVerdict:
    passed: bool
    text: str
    missing_labels: tuple[str, ...] = ()
    stray_numbers: tuple[str, ...] = ()
    stray_text: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def record(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "text": self.text.strip(),
            "missing_labels": list(self.missing_labels),
            "stray_numbers": list(self.stray_numbers),
            "stray_text": list(self.stray_text),
            "reasons": list(self.reasons),
        }


def _tokens(text: str) -> list[str]:
    return [t.strip(".,") for t in _TOKEN.findall(text) if t.strip(".,")]


def check_labels(text: str, labels: Sequence[str]) -> OcrVerdict:
    """`info` 씬 — 라벨이 전부 읽히고 계약에 없는 숫자가 없는가."""
    haystack = normalize(text)
    wanted = [str(label) for label in labels if str(label).strip()]
    missing = tuple(label for label in wanted if normalize(label) not in haystack)

    label_blobs = [normalize(label) for label in wanted]
    stray: list[str] = []
    for token in _tokens(text):
        if not any(ch.isdigit() for ch in token):
            continue
        norm = normalize(token)
        if any(norm in blob for blob in label_blobs):
            continue
        stray.append(token)

    reasons: list[str] = []
    if missing:
        reasons.append(f"라벨을 읽지 못했다: {', '.join(missing)}")
    if stray:
        reasons.append(f"계약에 없는 숫자가 있다: {', '.join(stray)}")
    return OcrVerdict(
        passed=not reasons, text=text, missing_labels=missing,
        stray_numbers=tuple(stray), reasons=tuple(reasons),
    )


def check_no_text(text: str) -> OcrVerdict:
    """`info` 없는 씬 — 글자가 없어야 통과다 (한 글자 잡음은 무시)."""
    found = tuple(t for t in _tokens(text) if len(t) >= 2)
    reasons = (f"글자가 있다: {', '.join(found[:6])}",) if found else ()
    return OcrVerdict(passed=not found, text=text, stray_text=found, reasons=reasons)


def gate(text: str, labels: Sequence[str] | None) -> OcrVerdict:
    """스펙 05 `[7]` ① — 라벨이 있으면 대조, 없으면 글자 금지."""
    if labels:
        return check_labels(text, labels)
    return check_no_text(text)
