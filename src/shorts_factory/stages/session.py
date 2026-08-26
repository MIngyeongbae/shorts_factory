"""헤드리스 세션 공용 배선. **판단은 없다.**

프롬프트를 읽고, 헤드리스 세션을 부르고, JSON을 회수하고, 계약 값을 프롬프트가 읽을
모양으로 옮긴다. 옛 대본 체인은 죽었지만(ADR-0049) 이 배선은
`[4] refpack`·`[1] draft`·`[2] factcheck`가 그대로 쓰고, `format_vocab`은
`[3s. scenetable]`이 쓴다. 단계끼리는 여전히 파일로만 통신한다 (ADR-0011, D-1).

## 값을 프롬프트에 손으로 적지 않는다

분량·어휘를 프롬프트 마크다운에 써 넣으면 `specs/schema/`와 갈라진다
(ADR-0034 §3). 프롬프트에는 `${limits}`·`${vocab}` 자리만 두고 **여기서 채운다.**
"""

from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any, Sequence

from ..jsonio import JSONExtractionError, extract_json_object
from ..llm.base import LLMClient
from ..schemas import vocab

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

#: 헤드리스 세션 타임아웃(초). 웹 도구 없이 생성만 하므로 조사 단계보다 짧다.
TIMEOUT = 600

#: 세션에 준 도구. 입력은 전부 프롬프트에 주입되므로 읽을 것이 없다 (ADR-0011).
TOOLS: tuple[str, ...] = ()


class ScriptSessionError(Exception):
    """대본 3단계 공통 예외. 어느 단계가 던졌는지는 메시지가 밝힌다."""


def load_prompt(name: str) -> Template:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise ScriptSessionError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def ask_json(
    llm: LLMClient,
    prompt: str,
    *,
    label: str,
    tools: Sequence[str] = TOOLS,
    timeout: int = TIMEOUT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """세션을 부르고 JSON 객체 하나를 회수한다. `(payload, meta)`."""
    result = llm.run(prompt, allowed_tools=tools, timeout=timeout, label=label)
    try:
        return extract_json_object(result.text), result.meta
    except JSONExtractionError as exc:
        raise ScriptSessionError(f"{label}: 세션 출력이 JSON 객체가 아니다 — {exc}") from exc


# --- 계약 → 프롬프트 -------------------------------------------------------

#: 분량 값의 사람용 이름. **값은 여기 없다** — script-rules.json에서 온다.
_LIMIT_LABELS = {
    "total_chars": "대본 전체 글자 수 (공백·문장부호 제외)",
    "line_count": "자막 줄 수 = 씬 수",
    "line_chars_target": "줄당 목표 글자 수",
    "line_chars_max": "줄당 최대 글자 수",
    "total_seconds": "영상 길이(초)",
    "speed_cps": "발화 속도(자/초)",
}


def format_limits(*keys: str, limits: dict[str, Any] | None = None) -> str:
    """분량 엔벨로프를 프롬프트 목록으로. 단계마다 필요한 것만 고른다.

    `limits`를 주면 그 dict에서 읽는다 — `[2l]`이 `vocab.locale_limits(lang)`을 넘기는
    자리다 (ADR-0056 결정 5). 기본은 ko 엔벨로프(`vocab.limits()`).
    """
    limits = vocab.limits() if limits is None else limits
    lines = []
    for key in keys:
        value = limits[key]
        shown = f"{value[0]}~{value[1]}" if isinstance(value, list) else f"{value}"
        lines.append(f"- **{_LIMIT_LABELS.get(key, key)}**: {shown}")
    return "\n".join(lines)


def _entries(name: str) -> list[tuple[str, dict[str, Any]]]:
    """어휘 하나의 (값, 설명). 순서는 `vocab.json`의 enum 순서다."""
    meta = vocab.meta(name)
    return [(value, meta.get(value, {})) for value in vocab.values(name)]


def format_vocab() -> str:
    """씬 계약이 고를 어휘 전부 (`[3s]` 몫 — ADR-0049). 출처는 `vocab.json` 하나다 (ADR-0033 §3)."""
    blocks: list[str] = []

    blocks.append("## beat (서사 기능의 라벨)\n")
    blocks.append(
        "\n".join(f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("beat"))
    )
    blocks.append("\n순서 제약도 필수 비트도 없다. 이 소재에 맞는 라벨을 고른다.\n")

    blocks.append("\n## subject_scale (피사체를 담는 크기)\n")
    blocks.append(
        "\n".join(
            f"- `{value}` — {item.get('gloss', '')}"
            for value, item in _entries("subject_scale")
        )
    )

    blocks.append("\n## framing (구도 토큰, 선택)\n")
    for scale in vocab.values("subject_scale"):
        tokens = [
            f"`{value}`({item.get('gloss', '')})"
            for value, item in _entries("framing")
            if item.get("scale") == scale
        ]
        if tokens:
            blocks.append(f"- **{scale}**: " + ", ".join(tokens))

    blocks.append("\n## camera (카메라 워크)\n")
    blocks.append(
        "\n".join(f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("camera"))
    )

    # motion은 없다 — 전 씬이 영상 클립이다 (ADR-0056). 어휘에서 삭제됐다.

    blocks.append("\n## staging (무대, 선택 — ADR-0056 결정 4)\n")
    blocks.append(
        "\n".join(
            f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("staging")
        )
    )

    blocks.append("\n## transition (이 씬으로 진입하는 전환, 선택)\n")
    blocks.append(
        "\n".join(
            f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("transition")
        )
    )

    blocks.append(
        "\n## info_device (info 씬에서 **정보를 지는 구도 장치** — ADR-0075 결정 5)\n"
    )
    blocks.append(
        "\n".join(
            f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("info_device")
        )
    )
    blocks.append(
        "\n`info` 씬의 정보는 빨간 표시가 아니라 **그림 자체**가 진다. 표시를 떼어도 그림이 "
        "여전히 그 씬의 `visual_goal`을 말해야 하고, 이 목록이 그 방법이다. 소재에 맞는 "
        "장치가 있으면 `info.device`에 고르고, 없으면 비운다 — 억지로 고르지 마라.\n"
    )

    blocks.append("\n## annotation (info의 계측 표시 방식 — ADR-0056 결정 3)\n")
    blocks.append(
        "\n".join(
            f"- `{value}` — {item.get('gloss', '')}" for value, item in _entries("annotation")
        )
    )
    blocks.append(
        "\n표시는 **구도 위의 주석**이다 (ADR-0075) — 이것 하나가 정보를 다 지게 두지 마라.\n"
    )

    blocks.append("\n## unit (info.labels에 쓰는 단위 기호 — ASCII만)\n")
    blocks.append(
        ", ".join(f"`{value}`({item.get('gloss', '')})" for value, item in _entries("unit"))
    )

    # emphasis는 여기 없다 — 오버레이 합성은 ADR-0054가 삭제했다. 화면에 세울
    # 숫자·라벨은 info(빨간 계측 표시 + 영어 라벨) 하나로 적는다.
    return "\n".join(blocks)
