"""[2l. localize] — 검증이 끝난 한국어 정본을 줄 1:1로 번안한다 (ADR-0056 결정 5).

specs/05-pipeline.md:
    [2l. localize] → topics/{slug}/script.ja.md + script.en.md
    (헤드리스 1회, 두 언어를 한 세션에 — 검증이 끝난 한국어 정본을 **줄 1:1 정렬**로
    번안한다. 줄 수·순서가 같아야 클립 풀을 공유한다. 직후 기계 검사: 줄 수 일치 +
    total_seconds 상한)

토픽 하나가 쇼츠 3편이 되는 구조다 — 세 언어가 클립 풀 하나를 공유하므로 줄(=자막 줄=씬,
ADR-0013)이 정본과 1:1이어야 한다. 규칙의 정본은 specs/01 「번안 대본」이고 프롬프트
(prompts/02l-localize.md)가 그것을 나른다.

- 입력은 `[2. factcheck]`가 끝난 `script.md`와 `factcheck.md`(수치·고유명사 표기 근거.
  없으면 경고만 — D-3). 세션에는 도구를 주지 않는다 — 입력은 전부 프롬프트에 주입하고
  산출물 파일은 오케스트레이터가 쓴다 (ADR-0011)
- **대본 파일이 이미 있으면 그 언어는 다시 만들지 않는다** — 사람이 고쳤을 수 있다.
  `--force`도 이것을 넘지 않는다. 한 언어만 다시 만들려면 그 파일을 지우고 돌린다
- 기계 검사(LLM 0회)는 `scriptmd.check_localized_script_md`. 통과한 언어만 파일을 쓴다 —
  ja가 통과하고 en이 실패하면 ja만 남는다. 실패는 보고·중단이고 재생성 루프는 없다
  (ADR-0044). 세션 원본은 `logs/`에 있다
- `runs/{run_id}/localize.json`은 언어별 결과의 **기록**이지 계약이 아니다. 2부는
  `script.{lang}.md`의 존재만 본다 (specs/05 경계 절)
- **읽기 절(`## 읽기`)도 같은 세션이 낸다** (ADR-0073). `speech-rules.json`에
  `reading_line` 블록이 있는 언어(지금은 ja)의 대본만 갖는 절이고, TTS 전용이라
  자막·화면에는 나가지 않는다. **읽기의 성패는 대본의 성패와 갈라 둔다** — 검사에
  걸리면 절만 빼고 대본은 쓴다. `[3]`이 원문을 보내는 것이 ADR-0063 이전의 동작이라
  이 계약은 한 방향으로만 움직인다. 대본이 이미 있고 절만 없으면 **절만 덧붙인다**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState, find_run_for_slug
from ..schemas import speech_rules, vocab
from ..schemas.script_rules import max_total_seconds
from ..schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE
from . import status as status_mod
from .factcheck import FACTCHECK_FILE, STAGE as FACTCHECK_STAGE
from .scriptmd import (
    append_reading_section,
    check_localized_script_md,
    check_reading_lines,
    format_limits,
    load_prompt,
    localized_mark,
    parse_script_md,
    reading_mark,
    requires_reading,
    script_md_name,
    split_localized_output,
    split_reading_output,
    strip_reading_section,
)
from .session import TOOLS

log = logging.getLogger(__name__)

STAGE = "2l-localize"
PROMPT_FILE = "02l-localize.md"
RECORD_FILE = "localize.json"

#: 두 언어를 한 세션에 쓴다 — 생성만이라 조사 단계보다 짧되 대본 두 벌이다.
TIMEOUT = 900

#: 번안 대상 — 정본 언어를 뺀 나머지 (specs/05 `{lang}` ∈ ko·ja·en).
TARGET_LANGUAGES: tuple[str, ...] = tuple(l for l in LANGUAGES if l != PRIMARY_LANGUAGE)

#: 언어별 결과 상태 (`localize.json`·`state.json`에 남는 값)
WRITTEN = "written"
EXISTING = "existing"
FAILED = "failed"
MISSING = "missing"


class LocalizeStageError(Exception):
    pass


@dataclass
class LanguageOutcome:
    lang: str
    status: str
    path: Path | None = None
    line_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: `## 읽기` 절의 상태 (ADR-0073). 읽기를 요구하지 않는 언어는 `None`이다.
    #: **대본의 성패와 갈라 둔다** — 읽기가 실패해도 대본은 살고 `[3]`이 원문을 보낸다
    #: (결정 6). 그래서 `ok`는 이 값을 보지 않는다.
    reading: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (WRITTEN, EXISTING)


@dataclass
class LocalizeResult:
    topic: str
    slug: str
    run_id: str
    outcomes: list[LanguageOutcome] = field(default_factory=list)
    record_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def errors(self) -> list[str]:
        return [error for outcome in self.outcomes for error in outcome.errors]

    @property
    def passed(self) -> bool:
        return not self.errors

    def outcome(self, lang: str) -> LanguageOutcome | None:
        return next((o for o in self.outcomes if o.lang == lang), None)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        parts = []
        for outcome in self.outcomes:
            labels = {
                WRITTEN: "생성", EXISTING: "기존 유지", FAILED: "검사 실패", MISSING: "세션 누락",
            }
            label = labels[outcome.status]
            if outcome.reading is not None:
                # 읽기는 대본과 갈라 보고한다 — 실패해도 대본은 살기 때문이다 (ADR-0073)
                label += f" / 읽기 {labels[outcome.reading]}"
            parts.append(f"{outcome.lang} {label}")
        detail = ", ".join(parts) or "대상 언어 없음"
        if self.passed:
            return f"[2l] {self.topic} — 번안 대본 ({detail}){tail}"
        return (
            f"[2l] {self.topic} — 번안 검사 실패 ({detail}). "
            "실패한 언어의 파일은 쓰지 않았다 — 다시 돌릴지는 사람이 정한다 (ADR-0044)"
            f"{tail}"
        )


def _targets(langs: Sequence[str] | None) -> list[str]:
    if langs is None:
        return list(TARGET_LANGUAGES)
    chosen: list[str] = []
    for lang in langs:
        lang = lang.strip().lower()
        if lang == PRIMARY_LANGUAGE:
            raise LocalizeStageError(
                f"'{PRIMARY_LANGUAGE}'는 정본 언어라 번안 대상이 아니다 "
                f"(가능: {', '.join(TARGET_LANGUAGES)})"
            )
        if lang not in TARGET_LANGUAGES:
            raise LocalizeStageError(
                f"모르는 언어 '{lang}' (가능: {', '.join(TARGET_LANGUAGES)})"
            )
        if lang not in chosen:
            chosen.append(lang)
    if not chosen:
        raise LocalizeStageError("번안 대상 언어가 비어 있다")
    return chosen


def _format_language_limits(lang: str) -> str:
    """프롬프트에 실을 언어별 분량 — 값은 전부 `script-rules.json`에서 온다 (ADR-0034)."""
    limits = vocab.locale_limits(lang)
    bound = max_total_seconds(lang)
    if limits:
        keys = [key for key in limits if not key.startswith("_")]
        body = format_limits(*keys, limits=limits)
        return f"**{lang}** (기계가 검사한다 — script-rules.json locales.{lang}.limits):\n{body}"
    return (
        f"**{lang}**: 언어별 엔벨로프가 아직 없다 — 기계는 줄 수 일치만 검사하고, "
        f"총 길이 상한 {bound:g}초는 [3] tts가 실측으로 본다. 하한은 없다"
    )


def _format_output_blocks(targets: Sequence[str], readings: Sequence[str]) -> str:
    blocks = [
        f"{localized_mark(lang)}\n\n{{{script_md_name(lang)} 전문 — 첫 글자는 `#`}}"
        for lang in targets
    ]
    blocks += [
        f"{reading_mark(lang)}\n\n{{읽기 줄만 — 마크다운 없이 한 줄에 하나}}"
        for lang in readings
    ]
    return "\n\n".join(blocks)


#: 읽기 절 규칙의 프롬프트 라벨. **값은 계약이 준다** (`speech-rules.json`의
#: `locales.{lang}.reading_line`) — 여기 있는 것은 사람이 읽는 이름뿐이다 (ADR-0034 §3).
_READING_LABELS = {
    "kana": "가나 종류 (한자를 이 가나로 편다)",
    "keep_katakana": "가타카나 외래어는 그대로 둔다",
    "kanji_only_after_digit": "한자를 남길 수 있는 자리는 숫자 바로 뒤뿐이다",
    "kanji_window_after_digit": "숫자 뒤 몇 글자까지 한자를 허용하는가 (기계 검사의 창)",
}


def _format_reading_rules(langs: Sequence[str]) -> str:
    """읽기 절 규칙 — 값은 전부 `speech-rules.json`에서 온다 (ADR-0073)."""
    blocks: list[str] = []
    for lang in langs:
        rules = speech_rules.reading_line(lang) or {}
        body = "\n".join(
            f"- **{_READING_LABELS.get(key, key)}**: `{value}`" for key, value in rules.items()
        )
        blocks.append(
            f"**{lang}** (기계가 검사한다 — speech-rules.json "
            f"locales.{lang}.reading_line):\n{body}"
        )
    return "\n\n".join(blocks)


def _reading_targets(
    outcomes: dict[str, LanguageOutcome], paths_by_lang: dict[str, Path]
) -> list[str]:
    """대본은 있는데 `## 읽기` 절만 없는 언어 (ADR-0073 결정 7).

    이미 있는 읽기 절은 건드리지 않는다 — `--force`도 넘지 않는다. 사람이 고쳤을 수 있다.
    """
    todo: list[str] = []
    for lang, outcome in outcomes.items():
        if not requires_reading(lang) or outcome.reading is not None:
            continue
        path = paths_by_lang[lang]
        if not path.exists():
            continue
        if not parse_script_md(path.read_text(encoding="utf-8")).has_reading_section:
            todo.append(lang)
    return todo


def _format_existing_scripts(langs: Sequence[str], outputs: dict[str, Path]) -> str:
    """읽기 절만 만들 언어의 **기존 대본 전문**. 세션이 그 줄을 보고 읽기를 쓴다."""
    if not langs:
        return "(없음 — 이번에 만드는 대본에서 바로 읽기를 쓴다)"
    blocks = []
    for lang in langs:
        text = outputs[lang].read_text(encoding="utf-8")
        blocks.append(f"### {script_md_name(lang)} (이미 있는 대본 — 고치지 마라)\n\n{text}")
    return "\n\n".join(blocks)


def _accept_reading(text: str, *, lang: str) -> tuple[str, str, list[str]]:
    """세션이 낸 대본 안의 `## 읽기` 절을 판정한다 → (대본 전문, 상태, 경고).

    걸리면 **절만 들어내고 대본은 그대로 돌려준다** (ADR-0073 결정 6). 오류를 경고로
    낮추는 자리이기도 하다 — 읽기의 실패가 대본의 실패가 아니기 때문이다.
    """
    doc = parse_script_md(text)
    if not doc.has_reading_section:
        return text, MISSING, [f"{lang}: 세션이 `## 읽기` 절을 내지 않았다 — [3]이 원문을 읽는다"]

    errors, warnings = check_reading_lines(doc.reading, doc.lines, lang=lang)
    if errors:
        return (
            strip_reading_section(text),
            FAILED,
            [*warnings, *errors, f"{lang}: 읽기 절을 빼고 대본만 썼다 — [3]이 원문을 읽는다"],
        )
    return text, WRITTEN, warnings


def _fill_reading_section(
    lang: str, *, path: Path, lines: list[str] | None, outcome: LanguageOutcome
) -> None:
    """이미 있는 대본에 `## 읽기` 절만 덧붙인다 (ADR-0073 결정 7).

    `## 대본`·`## 주장`의 바이트는 손대지 않는다 — 재생성이 아니라 빠진 절을 채우는
    것이라 사람이 고친 대본이 그대로 산다. 실패는 경고이고 대본은 건드리지 않는다.
    """
    if lines is None:
        outcome.reading = MISSING
        outcome.warnings.append(f"{lang}: 세션이 {reading_mark(lang)} 절을 내지 않았다")
        return

    text = path.read_text(encoding="utf-8")
    doc = parse_script_md(text)
    errors, warnings = check_reading_lines(lines, doc.lines, lang=lang)
    outcome.warnings.extend(warnings)
    if errors:
        outcome.reading = FAILED
        outcome.warnings.extend(errors)
        outcome.warnings.append(f"{lang}: 읽기 절을 붙이지 않았다 — [3]이 원문을 읽는다")
        return

    write_text(path, append_reading_section(text, lines))
    outcome.reading = WRITTEN


def run_localize_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    langs: Sequence[str] | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
) -> LocalizeResult:
    paths = paths or Paths.from_env()
    targets = _targets(langs)

    if run_id:
        import json

        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise LocalizeStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    script_path = topic_dir / script_md_name(PRIMARY_LANGUAGE)
    outputs = {lang: topic_dir / script_md_name(lang) for lang in targets}
    record_path = run_dir / RECORD_FILE

    # 읽기 절이 빠진 대본이 남아 있으면 완료 상태여도 스킵하지 않는다 (ADR-0073 결정 7) —
    # 대본은 그대로 두고 절만 채우러 들어간다. 이 조건이 없으면 ADR-0073 이전에 만든
    # 대본들이 영영 읽기를 못 갖는다.
    reading_missing = [
        lang
        for lang, path in outputs.items()
        if requires_reading(lang)
        and path.exists()
        and not parse_script_md(path.read_text(encoding="utf-8")).has_reading_section
    ]
    if (
        state.is_done(STAGE)
        and not force
        and not reading_missing
        and all(p.exists() for p in outputs.values())
    ):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return LocalizeResult(
            topic=topic, slug=slug, run_id=run_id, record_path=record_path, skipped=True,
            outcomes=[
                LanguageOutcome(
                    lang, EXISTING, path=path,
                    reading=EXISTING if requires_reading(lang) else None,
                )
                for lang, path in outputs.items()
            ],
        )

    if not script_path.exists():
        raise LocalizeStageError(
            f"대본이 없다: {script_path}. [1. draft]·[2. factcheck]를 먼저 실행하라."
        )
    if not state.is_done(FACTCHECK_STAGE):
        raise LocalizeStageError(
            f"[2. factcheck]가 끝나지 않았다 (state.json {FACTCHECK_STAGE}: "
            f"{state.status_of(FACTCHECK_STAGE)}). 번안은 검증이 끝난 정본에서만 한다 (specs/05)."
        )

    script_text = script_path.read_text(encoding="utf-8")
    ko_lines = len(parse_script_md(script_text).lines)
    if ko_lines == 0:
        raise LocalizeStageError(f"정본의 `## 대본` 절이 비어 있다: {script_path}")

    warnings: list[str] = []
    factcheck_path = topic_dir / FACTCHECK_FILE
    if factcheck_path.exists():
        factcheck_text = factcheck_path.read_text(encoding="utf-8")
    else:
        factcheck_text = "(factcheck.md 없음 — 정본의 `## 주장` 절과 본문의 표기를 따른다)"
        warnings.append(
            f"{FACTCHECK_FILE}이 없다 — 수치·고유명사 표기 근거 없이 번안한다 (D-3)"
        )

    outcomes: dict[str, LanguageOutcome] = {}
    for lang in targets:
        path = outputs[lang]
        if path.exists():
            # 사람이 고쳤을 수 있다 — 다시 만들지 않는다 (specs/05 `[2l]`). 지우면 재생성.
            doc = parse_script_md(path.read_text(encoding="utf-8"))
            existing_lines = len(doc.lines)
            outcomes[lang] = LanguageOutcome(
                lang, EXISTING, path=path, line_count=existing_lines,
                reading=(EXISTING if doc.has_reading_section else None)
                if requires_reading(lang)
                else None,
            )
            if existing_lines != ko_lines:
                outcomes[lang].warnings.append(
                    f"{lang}: 기존 {path.name}이 {existing_lines}줄 — 정본 {ko_lines}줄과 다르다. "
                    "덮어쓰지 않는다 — 지우고 다시 돌리거나 손으로 맞춰라"
                )
    todo = [lang for lang in targets if lang not in outcomes]
    # 대본은 그대로 두고 읽기 절만 채우는 언어 (ADR-0073 결정 7)
    reading_todo = _reading_targets(outcomes, outputs)
    reading_langs = [lang for lang in targets if requires_reading(lang)]

    if todo or reading_todo:
        state.mark_running(STAGE)
        try:
            prompt = load_prompt(PROMPT_FILE).substitute(
                topic=topic,
                targets=", ".join(todo) or "(없음 — 대본은 전부 이미 있다)",
                line_count=ko_lines,
                script=script_text,
                factcheck=factcheck_text,
                limits="\n\n".join(_format_language_limits(lang) for lang in todo)
                or "(대본을 새로 만들 언어가 없다)",
                reading_langs=", ".join(reading_langs) or "(없음)",
                reading_rules=_format_reading_rules(reading_langs)
                or "(읽기 절을 요구하는 언어가 없다)",
                reading_only=", ".join(reading_todo) or "(없음)",
                existing_scripts=_format_existing_scripts(reading_todo, outputs),
                output_blocks=_format_output_blocks(todo, reading_todo),
            )
            result = llm.run(prompt, allowed_tools=TOOLS, timeout=timeout, label=STAGE)
            sections = split_localized_output(result.text)
            readings = split_reading_output(result.text)
        except Exception as exc:
            state.mark_failed(STAGE, f"{type(exc).__name__}: {exc}")
            raise

        for lang in reading_todo:
            _fill_reading_section(
                lang, path=outputs[lang], lines=readings.get(lang), outcome=outcomes[lang]
            )

        for lang in todo:
            path = outputs[lang]
            text = sections.get(lang)
            if text is None:
                outcomes[lang] = LanguageOutcome(
                    lang, MISSING, path=path,
                    errors=[f"{lang}: 세션이 {localized_mark(lang)} 절을 내지 않았다"],
                )
                continue
            errors, lang_warnings = check_localized_script_md(
                text, lang=lang, expected_lines=ko_lines
            )
            line_count = len(parse_script_md(text).lines)
            if errors:
                # 통과하지 못한 파일은 쓰지 않는다 — 깨진 대본이 남으면 [3]이 파일이
                # 있다는 이유로 그 언어를 돈다. 세션 원본은 logs/에 있다.
                outcomes[lang] = LanguageOutcome(
                    lang, FAILED, path=path, line_count=line_count,
                    errors=errors, warnings=lang_warnings,
                )
                continue
            reading_status: str | None = None
            if requires_reading(lang):
                # 읽기가 걸려도 대본은 쓴다 — 읽기 절만 들어내고 경고로 남긴다
                # (ADR-0073 결정 6). [3]은 그러면 원문을 보내고 그것이 예전 동작이다.
                text, reading_status, reading_warnings = _accept_reading(text, lang=lang)
                lang_warnings = [*lang_warnings, *reading_warnings]
            write_text(path, text.rstrip() + "\n")
            outcomes[lang] = LanguageOutcome(
                lang, WRITTEN, path=path, line_count=line_count, warnings=lang_warnings,
                reading=reading_status,
            )

    ordered = [outcomes[lang] for lang in targets]
    record = {
        "stage": STAGE,
        "run_id": run_id,
        "slug": slug,
        "source": script_path.relative_to(paths.root).as_posix(),
        "source_lines": ko_lines,
        "session_called": bool(todo or reading_todo),
        "reading_only": list(reading_todo),
        "languages": {
            o.lang: {
                "status": o.status,
                "path": o.path.relative_to(paths.root).as_posix() if o.path else None,
                "line_count": o.line_count,
                "reading": o.reading,
                "errors": o.errors,
                "warnings": o.warnings,
            }
            for o in ordered
        },
        "warnings": warnings,
    }
    write_text(record_path, dump_json(record))

    info = {
        "record": record_path.relative_to(paths.root).as_posix(),
        "languages": {o.lang: o.status for o in ordered},
        "validation_errors": [e for o in ordered for e in o.errors],
        "validation_warnings": warnings + [w for o in ordered for w in o.warnings],
    }
    result = LocalizeResult(
        topic=topic, slug=slug, run_id=run_id, outcomes=ordered,
        record_path=record_path, warnings=list(info["validation_warnings"]),
    )

    failed = [o.lang for o in ordered if not o.ok]
    if failed:
        state.mark_failed(STAGE, f"번안 검사 실패: {', '.join(failed)}", **info)
        return result

    state.mark_done(STAGE, **info)
    status_mod.sync_checklist(topic_dir, state)
    return result
