"""[3. tts+sync] — 대본 → 나레이션 오디오 + 실측 타임스탬프, **언어당 1회**. 2부의 첫 단계다.

specs/05-pipeline.md:
    [3. tts+sync] → narration.{lang}.wav + timing.{lang}.json + scenes.timed.{lang}.json
                    (언어당 1회 — ko 필수, ja·en은 대본 파일이 있으면. ElevenLabs
                     with-timestamps 문자 정렬 → 문장 경계 실측, ADR-0004. 목소리는
                     언어별 ELEVEN_VOICE_ID[_JA|_EN], ADR-0056 결정 7)

    "대본 파일이 있는데 그 언어의 id가 비어 있으면 진입 전에 멈춘다 — 비싼 호출 전의
     설정 오류다. … ja·en의 씬 수가 ko와 다르면 멈춘다 — [2l]의 줄 정렬이 깨진 것이다.
     … 총 길이가 total_seconds 상한을 넘으면 그 언어의 대본 축약이 필요하다고 리포트하고
     멈춘다. atempo는 스펙 04의 상한 안에서 언어별로 건다."

## 경계 (ADR-0017 — ADR-0049·0052·0056)

입력은 `topics/{slug}/script.md`(ko, 필수)·`script.ja.md`·`script.en.md`(있는 것만, D-3)이고
전부 **읽기 전용**이다. 대본 줄이 곧 씬 경계다 (ADR-0013). `run_id`는 `runs/*/topic.json`
에서 슬러그로 찾는다. 산출물은 전부 `runs/{run_id}/` 아래이고 `scenes.timed.{lang}.json`은
`text`+실측 시각만 담는다 — 대본 속성은 `[3s]`의 `scenes.json` 소관이다.

## 돈이 나가기 전에 전부 확인한다

세 언어의 호출은 각각 과금이다. 그래서 **어느 언어도 부르기 전에** (1) 있는 대본의 줄 수가
ko와 같은지, (2) 있는 언어마다 voice_id가 채워져 있는지를 본다. 둘째 언어에서 멈추면 첫째
언어의 과금이 헛되다.

## 길이 초과 시 왜 멈추는가

여기서 할 수 있는 일이 없다. 배속을 더 올리는 것은 스펙 04의 상한 밖으로 나가는 결정이고,
대본을 줄이는 것은 1부 소관이다 (ADR-0017 단방향 경계). 그래서 `narration.{lang}.wav`와
`timing.{lang}.json`은 남기고(다시 사지 않아도 되게) `scenes.timed.{lang}.json`은 쓰지
않는다 — 하류가 계약 파일을 보고 진행해 버리는 것을 막는다. **ko가 넘치면 ja·en은 부르지
않는다** — ko를 줄이면 번안도 다시 되고 그 TTS도 다시 사야 한다. ja·en이 넘치면 그 언어만
멈추고 나머지는 돈다 — 언어끼리는 독립이다.

## 읽는 텍스트는 보는 텍스트가 아니다 (ADR-0063)

TTS로 나가는 것은 대본 줄이 아니라 **발화형**이다 — 숫자·단위를 그 언어가 읽는 대로 편
텍스트다 (`12cm` → `십이 센티미터`). 자막이 쓰는 `scenes.timed.{lang}.json`의 `text`는
**원문 그대로**이고, 대본 파일도 건드리지 않는다. 두 텍스트가 갈리는 자리는 여기뿐이다.

정렬 대조도 발화형 위에서 돈다 — 보낸 텍스트와 `alignment`가 글자까지 같아야 하기
때문이다 (`sync.character_spans`). 무엇을 어떻게 폈는지는 `timing.{lang}.json`의
`spoken`에 남는다. 사전에 없는 단위는 그대로 나가고 경고만 남는다 (`tts/speech.py`).

## 이 단계가 하지 않는 것

- **게이트 판정.** `judgment/human.json`의 `decision: go` 확인은 2부 진입점의 몫이다
- 자막 파일(ASS) 생성. `[9. assemble]`이 그 언어의 실측 파일로 만든다 (ADR-0020)
- 대본 수정. 오차가 크든 길이가 넘치든 대본은 1부 것이다
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunNotFound, RunState, find_run_for_slug
from ..schemas.script_rules import max_total_seconds
from ..schemas.timed_scenes import (
    LANGUAGES,
    PRIMARY_LANGUAGE,
    build_line_timed_scenes,
    timed_scenes_path,
    validate_line_timed_scenes,
)
from .scriptmd import parse_script_md
from ..tts.audio import DEFAULT_FFMPEG, DEFAULT_TEMPO, AudioError, write_narration
from ..tts.base import Narration, TTSClient
from ..tts.speech import SpokenScript, spoken_lines
from ..tts.sync import (
    LINE_JOINER,
    SyncError,
    narration_text,
    scale,
    scene_boundaries,
)

log = logging.getLogger(__name__)

STAGE = "3-tts-sync"

#: 경계면 파일 (ADR-0049·0052·0056) — 대본 줄이 곧 씬 경계다 (ADR-0013).
SCRIPT_MD_FILE = "script.md"
SCRIPT_MD_PATTERN = "script.{lang}.md"

NARRATION_PATTERN = "narration.{lang}.wav"
TIMING_PATTERN = "timing.{lang}.json"

#: specs/01 총 길이 상한 (ko). 언어별 상한은 `max_total_seconds(lang)`이다.
MAX_TOTAL_SECONDS = max_total_seconds(PRIMARY_LANGUAGE)

#: 실측 오디오 길이와 씬 마지막 end의 허용 차이(초). 넘으면 [9]의 싱크가 흔들린다.
AUDIO_TOLERANCE = 0.5

#: TTS 단일 호출 타임아웃(초). 570자 한 편 분량 기준.
TIMEOUT = 300

TTSFactory = Callable[[str], TTSClient]


class TTSStageError(Exception):
    pass


def script_path(paths: Paths, slug: str, lang: str) -> Path:
    """그 언어의 대본 경로 — ko는 `script.md`, 나머지는 `script.{lang}.md` (specs/05 경계 절)."""
    name = SCRIPT_MD_FILE if lang == PRIMARY_LANGUAGE else SCRIPT_MD_PATTERN.format(lang=lang)
    return paths.topic_dir(slug) / name


def outputs_for(run_dir: Path, lang: str) -> tuple[Path, Path, Path]:
    """`(narration, timing, scenes.timed)` — 언어당 세 파일."""
    return (
        run_dir / NARRATION_PATTERN.format(lang=lang),
        run_dir / TIMING_PATTERN.format(lang=lang),
        timed_scenes_path(run_dir, lang),
    )


@dataclass
class LanguageResult:
    lang: str
    tempo: float = DEFAULT_TEMPO
    scene_count: int = 0
    raw_duration: float = 0.0
    total_duration: float = 0.0
    audio_duration: float = 0.0
    narration_path: Path | None = None
    timing_path: Path | None = None
    scenes_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    over_length: bool = False
    skipped: bool = False
    max_seconds: float = MAX_TOTAL_SECONDS

    @property
    def passed(self) -> bool:
        return self.scenes_path is not None

    @property
    def line(self) -> str:
        head = (
            f"{self.lang} {self.scene_count}씬 / {self.raw_duration:.1f}초 원속 → "
            f"atempo {self.tempo} → {self.total_duration:.1f}초"
        )
        if self.over_length:
            return f"{head} → 상한 {self.max_seconds:.0f}초 초과, 중단 (대본 축약은 1부 소관)"
        return f"{head}{' (스킵)' if self.skipped else ''}"


@dataclass
class TTSResult:
    topic: str
    slug: str
    run_id: str
    run_dir: Path
    languages: dict[str, LanguageResult] = field(default_factory=dict)

    @property
    def primary(self) -> LanguageResult:
        return self.languages[PRIMARY_LANGUAGE]

    # --- ko 편의 속성 (옛 단일 언어 모양) ---------------------------------
    @property
    def tempo(self) -> float:
        return self.primary.tempo

    @property
    def scene_count(self) -> int:
        return self.primary.scene_count

    @property
    def raw_duration(self) -> float:
        return self.primary.raw_duration

    @property
    def total_duration(self) -> float:
        return self.primary.total_duration

    @property
    def audio_duration(self) -> float:
        return self.primary.audio_duration

    @property
    def narration_path(self) -> Path | None:
        return self.primary.narration_path

    @property
    def timing_path(self) -> Path | None:
        return self.primary.timing_path

    @property
    def scenes_path(self) -> Path | None:
        return self.primary.scenes_path

    @property
    def warnings(self) -> list[str]:
        return [
            w if lang == PRIMARY_LANGUAGE and len(self.languages) == 1 else f"[{lang}] {w}"
            for lang, result in self.languages.items() for w in result.warnings
        ]

    @property
    def over_length(self) -> bool:
        return any(r.over_length for r in self.languages.values())

    @property
    def over_length_languages(self) -> list[str]:
        return [lang for lang, r in self.languages.items() if r.over_length]

    @property
    def skipped(self) -> bool:
        return bool(self.languages) and all(r.skipped for r in self.languages.values())

    @property
    def passed(self) -> bool:
        return bool(self.languages) and all(r.passed for r in self.languages.values())

    @property
    def summary(self) -> str:
        lines = " · ".join(r.line for r in self.languages.values())
        if self.over_length:
            return (
                f"[3] {self.topic} — {lines} → 중단 — "
                f"{', '.join(self.over_length_languages)} 대본 축약이 필요하다 (1부 소관)"
            )
        return f"[3] {self.topic} — {lines} → scenes.timed.{{{','.join(self.languages)}}}.json"


def build_timing(
    *,
    source: dict[str, Any],
    lang: str,
    boundaries: list[tuple[float, float]],
    narration_meta: dict[str, Any],
    tempo: float,
    raw_duration: float,
    audio_duration: float,
    warnings: list[str],
    spoken: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """timing.{lang}.json 문서 — **이 단계의 실행 기록이다** (ADR-0020).

    하류 단계가 판단 근거로 읽는 계약이 아니다. 씬의 시각을 읽는 곳은 그 언어의
    `scenes.timed.{lang}.json` 하나뿐이고, 여기 남는 것은 (1) 엔진 메타, (2) 배속과 원속
    길이 — 계산을 재현할 근거, (3) 경고, (4) 총 길이 — **길이 초과로 멈춰 실측 파일을 쓰지
    않는 경우에도** 무엇이 얼마나 넘쳤는지 남기기 위한 것이다. 호출은 언어당 과금이다.

    (5) `spoken` — 발화형으로 편 줄과, 엔진이 그 위에 무엇을 더 읽었는지
    (`normalized_alignment`). ADR-0063 되돌릴 조건 1의 관측 수단이다: 오디오를 다시 듣지
    않고 무엇을 어떻게 읽혔는지 여기서 본다. 편 줄이 없으면 키를 넣지 않는다.
    """
    document: dict[str, Any] = {
        "run_id": source["run_id"],
        "topic": source["topic"],
        "lang": lang,
        "engine": narration_meta,
        "tempo": tempo,
        "raw_duration": round(raw_duration, 3),
        "total_duration": boundaries[-1][1] if boundaries else 0.0,
        "audio": {
            "path": NARRATION_PATTERN.format(lang=lang),
            "duration": round(audio_duration, 3),
        },
        "warnings": warnings,
    }
    if spoken:
        document["spoken"] = spoken
    return document


def _tempo_for(tempo: float | Mapping[str, float], lang: str) -> float:
    if isinstance(tempo, Mapping):
        return float(tempo.get(lang, tempo.get(PRIMARY_LANGUAGE, DEFAULT_TEMPO)))
    return float(tempo)


def _client_for(tts: TTSClient | TTSFactory, lang: str) -> TTSClient:
    if isinstance(tts, TTSClient):
        return tts
    return tts(lang)


def _read_script(path: Path, lang: str) -> tuple[str, list[str]]:
    """그 언어 대본 → `(제목, 대본 줄)`.

    제목은 `# ` 머리글이고 **선택이다** — 없으면 빈 문자열이고 그 언어 영상에 제목
    훅이 안 붙는다 (ADR-0065, D-3). 대본 줄이 없는 것은 여전히 실패다.
    """
    doc = parse_script_md(path.read_text(encoding="utf-8"))
    lines = list(doc.lines)
    if not lines:
        raise TTSStageError(f"{path}에 대본 줄이 없다 (스펙 01 포맷 확인)")
    return doc.title, lines


def _language_state(state: RunState, lang: str) -> dict[str, Any]:
    return state.stage(STAGE).setdefault("languages", {}).setdefault(lang, {"status": "pending"})


def run_tts_stage(
    slug: str,
    *,
    tts: TTSClient | TTSFactory,
    paths: Paths | None = None,
    tempo: float | Mapping[str, float] = DEFAULT_TEMPO,
    langs: Sequence[str] | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> TTSResult:
    paths = paths or Paths.from_env()

    # --- 입력: 있는 대본만, ko는 필수 (D-3) ------------------------------------
    ko_path = script_path(paths, slug, PRIMARY_LANGUAGE)
    if not ko_path.exists():
        raise TTSStageError(
            f"대본이 없다: {ko_path}. 1부가 끝난 토픽만 2부에 들어온다 (ADR-0017)."
        )
    wanted = [l for l in (langs or LANGUAGES)]
    unknown = [l for l in wanted if l not in LANGUAGES]
    if unknown:
        raise TTSStageError(f"모르는 언어다: {unknown} (가능: {', '.join(LANGUAGES)})")
    if PRIMARY_LANGUAGE not in wanted:
        wanted.insert(0, PRIMARY_LANGUAGE)
    present = [l for l in LANGUAGES if l in wanted and script_path(paths, slug, l).exists()]

    scripts_by_lang: dict[str, tuple[str, list[str]]] = {
        lang: _read_script(script_path(paths, slug, lang), lang) for lang in present
    }
    texts_by_lang: dict[str, list[str]] = {
        lang: lines for lang, (_title, lines) in scripts_by_lang.items()
    }
    ko_count = len(texts_by_lang[PRIMARY_LANGUAGE])
    for lang in present:
        if len(texts_by_lang[lang]) != ko_count:
            raise TTSStageError(
                f"{script_path(paths, slug, lang).name}의 줄 수({len(texts_by_lang[lang])})가 "
                f"{SCRIPT_MD_FILE}({ko_count})과 다르다 — [2l]의 줄 1:1 정렬이 깨졌다. "
                "클립 풀을 공유할 수 없으므로 호출 전에 멈춘다 (ADR-0056 결정 5)"
            )

    try:
        run_id, run_contract = find_run_for_slug(paths, slug)
    except RunNotFound as exc:
        raise TTSStageError(str(exc)) from exc
    topic = str(run_contract.get("topic") or slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    result = TTSResult(topic=topic, slug=slug, run_id=run_id, run_dir=run_dir)

    # --- 어느 언어를 실제로 부를지 (재실행 스킵) ---------------------------------
    to_run: list[str] = []
    clients: dict[str, TTSClient] = {}
    for lang in present:
        narration_path, timing_path, scenes_path = outputs_for(run_dir, lang)
        lang_state = _language_state(state, lang)
        if (
            lang_state.get("status") == "done" and not force
            and all(p.exists() for p in (narration_path, timing_path, scenes_path))
        ):
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            result.languages[lang] = LanguageResult(
                lang=lang, tempo=timing.get("tempo", _tempo_for(tempo, lang)),
                scene_count=ko_count, raw_duration=timing.get("raw_duration", 0.0),
                total_duration=timing.get("total_duration", 0.0),
                audio_duration=timing.get("audio", {}).get("duration", 0.0),
                narration_path=narration_path, timing_path=timing_path, scenes_path=scenes_path,
                warnings=timing.get("warnings", []), skipped=True,
                max_seconds=max_total_seconds(lang),
            )
            log.info("[%s] %s는 이미 완료돼 스킵한다 (run_id=%s)", STAGE, lang, run_id)
            continue
        to_run.append(lang)
        clients[lang] = _client_for(tts, lang)

    # --- 돈이 나가기 전에 설정을 전부 본다 (스펙 05 [3]) ------------------------
    for lang in to_run:
        clients[lang].check_configured()  # TTSNotConfigured — 호출 전, 과금 없음

    if not to_run:
        return result

    state.mark_running(STAGE)

    for lang in to_run:
        if result.over_length_languages and PRIMARY_LANGUAGE in result.over_length_languages:
            # ko가 넘치면 번안도 다시 되므로 ja·en을 사지 않는다 (모듈 독스트링).
            log.warning("[%s] ko 길이 초과로 %s는 부르지 않는다", STAGE, lang)
            continue
        result.languages[lang] = _run_language(
            lang=lang, texts=texts_by_lang[lang], client=clients[lang], state=state,
            title=scripts_by_lang[lang][0],
            run_id=run_id, topic=topic, run_dir=run_dir, paths=paths,
            tempo=_tempo_for(tempo, lang), ffmpeg=ffmpeg, runner=runner,
        )

    # --- 단계 상태 ----------------------------------------------------------
    langs_info = state.stage(STAGE).get("languages", {})
    info: dict[str, Any] = {
        "languages": langs_info,
        "scene_count": ko_count,
        "warnings": result.warnings,
        "outputs": sorted(
            p for lang_info in langs_info.values() for p in lang_info.get("outputs", [])
        ),
    }
    if result.over_length:
        message = (
            f"총 길이가 상한을 넘는 언어가 있다: {', '.join(result.over_length_languages)}. "
            f"대본 축약이 필요하다 — 1부에서 그 언어의 대본을 다시 만들어야 한다 (ADR-0017). "
            "그 언어의 scenes.timed 파일은 쓰지 않았다"
        )
        state.mark_failed(STAGE, message, **info)
        log.warning("[%s] %s", STAGE, message)
    elif result.passed:
        state.mark_done(STAGE, **info)
    return result


def _spoken_record(speech: SpokenScript, narration: Narration) -> dict[str, Any] | None:
    """`timing.{lang}.json`의 `spoken` 블록 (ADR-0063 결정 5).

    편 줄이 없고 엔진 정규화도 못 받았으면 키 자체를 넣지 않는다 — 빈 블록은 "폈는데
    아무것도 안 바뀌었다"와 "볼 것이 없다"를 구별해 주지 못한다.
    """
    record: dict[str, Any] = {}
    if speech.changes:
        record["lines"] = [dict(change) for change in speech.changes]
    engine = narration.raw.get("normalized_text")
    if engine:
        # 엔진이 우리 발화형 위에 무엇을 더 읽었는지 보는 유일한 창이다 (ADR-0063 맥락 2).
        record["engine_normalized"] = engine
    return record or None


def _run_language(
    *, lang: str, texts: list[str], client: TTSClient, state: RunState,
    run_id: str, topic: str, run_dir: Path, paths: Paths, title: str = "",
    tempo: float, ffmpeg: str, runner,
) -> LanguageResult:
    """언어 하나 — 단일 호출 → 경계 → atempo → 세 파일. 실패는 `TTSStageError`로 올린다."""
    narration_path, timing_path, scenes_path = outputs_for(run_dir, lang)
    lang_state = _language_state(state, lang)
    lang_state["status"] = "running"
    lang_state.pop("error", None)
    state.save()

    # 지난 실행이 남긴 계약 파일을 먼저 치운다. 이번 실행이 실패하거나 길이 초과로
    # 멈추면 새 오디오와 짝이 맞지 않는 옛 타임스탬프가 남고, 하류 단계는
    # 파일이 있다는 이유로 그대로 진행해 버린다.
    scenes_path.unlink(missing_ok=True)

    # 보내는 것은 대본 줄이 아니라 발화형이다 (ADR-0063). 원문은 scenes.timed로 간다.
    speech = spoken_lines(texts, lang)
    text = narration_text(speech.lines, joiner=LINE_JOINER)
    log.info(
        "[%s] %s 단일 호출 %d자(원문 %d자) / %d씬 — 발화형 %d줄 (ADR-0004·0063)",
        STAGE, lang, len(text), len(narration_text(texts, joiner=LINE_JOINER)),
        len(texts), speech.changed_count,
    )

    narration = client.synthesize(text, timeout=TIMEOUT, label=f"{STAGE}:{lang}")

    def fail(message: str) -> TTSStageError:
        lang_state["status"] = "failed"
        lang_state["error"] = message
        state.mark_failed(STAGE, f"[{lang}] {message}")
        return TTSStageError(f"[{lang}] {message}")

    try:
        # 정렬은 **보낸 텍스트**의 것이다 — 경계도 같은 텍스트 위에서 뽑는다 (ADR-0063 결정 2).
        raw_boundaries, sync_warnings = scene_boundaries(
            narration.alignment, speech.lines, joiner=LINE_JOINER
        )
    except SyncError as exc:
        raise fail(str(exc)) from exc
    warnings = [*speech.warnings, *sync_warnings]

    # specs/05 — atempo 적용 후 타임스탬프도 1/tempo 스케일 보정
    boundaries = scale(raw_boundaries, 1.0 / tempo)
    raw_duration = narration.raw_duration
    total_duration = boundaries[-1][1]

    try:
        audio_duration = write_narration(
            narration, narration_path, tempo=tempo, executable=ffmpeg, runner=runner
        )
    except AudioError as exc:
        raise fail(str(exc)) from exc

    if abs(audio_duration - total_duration) > AUDIO_TOLERANCE:
        warnings.append(
            f"{narration_path.name} 길이 {audio_duration:.2f}초와 씬 총 길이 "
            f"{total_duration:.2f}초의 차이가 {AUDIO_TOLERANCE}초를 넘는다"
        )

    timed = build_line_timed_scenes(run_id, topic, texts, boundaries, title=title)
    errors, timed_warnings = validate_line_timed_scenes(timed)
    warnings.extend(timed_warnings)

    # timing.{lang}.json은 길이 초과로 멈추는 경우에도 남긴다 — 호출은 과금이고,
    # 무엇이 얼마나 넘쳤는지는 이 파일에서 읽는다.
    timing = build_timing(
        source={"run_id": run_id, "topic": topic}, lang=lang,
        boundaries=boundaries, narration_meta=narration.meta,
        tempo=tempo, raw_duration=raw_duration, audio_duration=audio_duration,
        warnings=warnings, spoken=_spoken_record(speech, narration),
    )
    write_text(timing_path, dump_json(timing))

    for warning in warnings:
        log.warning("[%s] %s: %s", STAGE, lang, warning)

    bound = max_total_seconds(lang)
    result = LanguageResult(
        lang=lang, tempo=tempo, scene_count=len(texts), raw_duration=raw_duration,
        total_duration=total_duration, audio_duration=audio_duration,
        narration_path=narration_path, timing_path=timing_path, warnings=warnings,
        max_seconds=bound,
    )

    lang_state.update({
        "scene_count": len(texts),
        "tempo": tempo,
        "raw_duration": round(raw_duration, 3),
        "total_duration": total_duration,
        "audio_duration": round(audio_duration, 3),
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix() for p in (narration_path, timing_path)
        ],
    })

    if errors:
        # 우리가 만든 문서가 계약을 어겼다는 뜻이라 하류로 넘기지 않는다.
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise fail(f"{scenes_path.name}이 씬 계약을 위반한다:\n{listed}")

    if total_duration > bound:
        # specs/05 — 리포트하고 멈춘다. 재생성은 사람이 1부를 다시 실행해 판단한다.
        lang_state["status"] = "over_length"
        lang_state["error"] = (
            f"총 길이 {total_duration:.2f}초가 상한 {bound:.0f}초를 넘는다. "
            f"대본 축약이 필요하다 — 1부 소관 (ADR-0017). {scenes_path.name}은 쓰지 않았다"
        )
        state.save()
        result.over_length = True
        return result

    write_text(scenes_path, dump_json(timed))
    lang_state["outputs"].append(scenes_path.relative_to(paths.root).as_posix())
    lang_state["status"] = "done"
    state.save()
    result.scenes_path = scenes_path
    return result
