"""파이프라인 오케스트레이터 CLI.

    python run.py topic     [--topic 소재명] [--seed-url URL]  # [0] 시드 — 폴더·run 생성
    python run.py seedfetch --slug SLUG           # [0f] 시드 URL → seed-body.md (헤드리스 렌더, ADR-0061)
    python run.py draft     --slug SLUG           # [1] 시드 기사 → 통짜 대본 script.md
    python run.py factcheck --slug SLUG           # [2] 대본 주장 검증·정정 → factcheck.md
    python run.py localize  --slug SLUG [--lang ja,en]  # [2l] 정본 → script.ja.md + script.en.md (줄 1:1, ADR-0056)
    python run.py part1     [--topic 소재명] [--seed-url URL]  # [0]+[0f]+[1]+[2]+[2l] 연속 (ADR-0049·0056·0061)
    python run.py tts       --slug SLUG [--lang ko,ja,en]  # [2부] 대본 → narration.{lang}.wav + 실측 (언어당 1회)
    python run.py scenetable --slug SLUG          # [2부] ko 실측 줄 경계 → 씬 계약 scenes.json
    python run.py refpack   --slug SLUG           # [2부] 씬 계약 → 씬별 실사 참조 (사진 + 서술)
    python run.py prompt    --slug SLUG           # [2부] 씬 계약 → 씬별 영상 프롬프트 (ADR-0056)
    python run.py videogen  --slug SLUG           # [2부] [7] 씬당 텍스트→영상 클립 + 검수 (어댑터는 video_line — ADR-0059)
    python run.py ending    --slug SLUG           # [2부] 실사 참조 → 엔딩 실사 컷 (ADR-0055)
    python run.py assemble  --slug SLUG [--lang ko,ja,en]  # [2부] 클립+언어별 실측 → timeline.{lang}.mp4

ADR-0008에 따라 LLM 단계는 claude 헤드리스 서브프로세스로 실행된다.
`prompt`는 2부 단계이고 LLM도 네트워크도 쓰지 않는다 (순수 변환, ADR-0033 §3).
`imagegen`·`imagereview`·`info`·`motion`은 ADR-0056이 단계째 지웠다 — 이미지 단계가 없다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable

from .config import DEFAULT_BACKOFF_BASE, DEFAULT_MAX_RETRIES, Paths, load_dotenv
from .llm.claude_code import ClaudeCodeClient
from .stages.assemble import (
    AssembleStageError,
    resolve_run_id,
    run_assemble_stage,
)
from .stages.ending import (
    TIMEOUT as ENDING_TIMEOUT,
    EndingStageError,
    resolve_run_id as resolve_ending_run_id,
    run_ending_stage,
)
from .stages.prompt import (
    TIMEOUT as PROMPT_TIMEOUT,
    PromptStageError,
    run_prompt_stage,
)
from .stages.refpack import (
    TIMEOUT as REFPACK_TIMEOUT,
    RefpackStageError,
    resolve_run_id as resolve_refpack_run_id,
    run_refpack_stage,
    urllib_fetch,
)
from .runstate import RunNotFound, find_run_for_slug
from .stages.draft import DraftStageError, run_draft_stage
from .stages.seedfetch import SeedfetchStageError, run_seedfetch_stage
from .stages.scenetable import (
    TIMEOUT as SCENETABLE_TIMEOUT,
    ScenetableStageError,
    resolve_run_id as resolve_scenetable_run_id,
    run_scenetable_stage,
)
from .stages.factcheck import FactcheckStageError, run_factcheck_stage
from .stages.frames import (
    FramesStageError,
    ProviderRefused as FramesProviderRefused,
    SESSION_TIMEOUT as FRAMES_SESSION_TIMEOUT,
    resolve_run_id as resolve_frames_run_id,
    run_frames_stage,
)
from .stages.localize import (
    TARGET_LANGUAGES,
    LocalizeStageError,
    run_localize_stage,
)
from .stages.topic import TopicStageError, run_topic_stage
from .stages.tts import TTSStageError, run_tts_stage
from .stages.videogen import (
    REVIEW_FULL,
    REVIEW_MODES,
    DEFAULT_REVIEW_SLOTS as VIDEOGEN_REVIEW_SLOTS,
    SESSION_TIMEOUT as VIDEOGEN_SESSION_TIMEOUT,
    ProviderRefused,
    VideogenStageError,
    resolve_run_id as resolve_videogen_run_id,
    run_videogen_stage,
)
from .schemas import vocab
from .schemas.timed_scenes import LANGUAGES
from .imagegen.midjourney import MidjourneyClient
from .imagegen.nano_banana import NanoBananaClient
from .tts.audio import DEFAULT_TEMPO
from .tts.base import TTSClient, TTSError, TTSNotConfigured
from .tts.elevenlabs import ElevenLabsClient
from .tts.fake import FakeTTSClient
from .videogen.base import VideoClient
from .videogen.fake import FakeVideoClient
from .videogen.comfy_h3 import ComfyH3Client, ComfyH3FirstLastClient
from .videogen.midjourney import MidjourneyEndImageClient
from .videogen.omni import OmniClient
from .judgment import JudgmentError, read_video_line, slug_from_run_id

log = logging.getLogger("shorts_factory")


def _force_utf8_streams() -> None:
    """stdout/stderr를 UTF-8로 고정한다.

    Windows 콘솔의 기본 인코딩은 cp949(한국어 로캘)라, 요약문에 흔한 em dash나
    일부 한글이 섞이면 print가 UnicodeEncodeError로 죽는다. 실제로 첫 실전
    package 실행이 모든 산출물을 쓴 뒤 마지막 요약 출력에서 이걸로 넘어갔다.
    파이프라인 출력은 전부 한국어라 이건 예외가 아니라 기본값이다.

    errors="replace": 인코딩 하나 때문에 완료된 단계의 결과 보고를 잃지 않는다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pytest capsys 등 교체된 스트림
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 이미 detach된 스트림
            pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _make_client(args, log_dir: Path | None) -> ClaudeCodeClient:
    return ClaudeCodeClient(
        executable=args.claude_bin,
        model=args.model,
        max_retries=args.max_retries,
        backoff_base=args.backoff_base,
        log_dir=log_dir,
    )


def _cmd_topic(args, paths: Paths) -> int:
    result = run_topic_stage(
        args.topic, paths=paths, force=args.force,
        seed_url=getattr(args, "seed_url", None),
    )
    print(result.summary)
    if not result.accepted:
        print(
            "\n백로그 항목을 고치거나 소재를 반려하라 "
            "(specs/06-topic-research.md).",
            file=sys.stderr,
        )
        return 2
    return 0


#: 1부 기계 검사 실패의 종료 코드. `[1]`·`[2]`는 4(엔벨로프), `[2l]`은 5(줄 정렬·번안 검사) —
#: 고칠 자리가 다르다: 4는 ko 정본, 5는 번안 쪽이고 ko 정본은 그대로다.
ENVELOPE_FAILURE = 4
LOCALIZE_FAILURE = 5

#: `[0f]`가 시드 본문을 못 얻었을 때. **파이프라인을 세우는 코드가 아니다** (D-5) —
#: `part1`은 이 실패를 무시하고 `[1]`로 넘어가고(WebFetch 사다리가 있다), 단계를 따로
#: 부른 사람에게만 0이 아닌 값으로 알린다.
SEEDFETCH_FAILURE = 6


def _report(result, failure_code: int = ENVELOPE_FAILURE) -> int:
    """1부 단계 공통 출력. 검증 실패는 `failure_code`로 나가되 **산출물은 남긴다.**

    재생성 루프는 없다 (ADR-0044 원칙, ADR-0049) — 다시 돌릴지는 사람이
    script.md를 읽고 정한다.
    """
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  오류: {error}", file=sys.stderr)
    return failure_code if result.errors else 0


def _run_script_stage(
    args, paths: Paths, runner, *, failure_code: int = ENVELOPE_FAILURE, **extra
) -> int:
    run_id = args.run_id
    if not run_id:
        run_id, _ = find_run_for_slug(paths, args.slug)
    client = _make_client(args, paths.run_dir(run_id) / "logs")
    return _report(
        runner(args.slug, llm=client, paths=paths, run_id=run_id, force=args.force,
               **extra),
        failure_code,
    )


def _report_seedfetch(result) -> int:
    """`[0f]` 공통 출력. 실패는 경고로 끝난다 — 산출물이 없을 뿐 사다리가 남아 있다."""
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 0 if result.passed else SEEDFETCH_FAILURE


def _cmd_seedfetch(args, paths: Paths) -> int:
    """[0f] 시드 URL → seed-body.md (헤드리스 렌더, ADR-0061)."""
    result = run_seedfetch_stage(
        args.slug, paths=paths, run_id=args.run_id, force=args.force,
        browser=getattr(args, "browser", None),
    )
    return _report_seedfetch(result)


def _cmd_draft(args, paths: Paths) -> int:
    """[1] 시드 기사 → 통짜 대본 script.md (ADR-0049)."""
    return _run_script_stage(args, paths, run_draft_stage)


def _cmd_factcheck(args, paths: Paths) -> int:
    """[2] 대본이 쓴 주장만 검증·정정 → factcheck.md (ADR-0049)."""
    return _run_script_stage(args, paths, run_factcheck_stage)


def _cmd_localize(args, paths: Paths) -> int:
    """[2l] 검증 끝난 정본 → script.ja.md + script.en.md, 줄 1:1 (ADR-0056 결정 5).

    이미 있는 언어 파일은 건드리지 않는다 — 한 언어만 다시 만들려면 그 파일을 지우고
    돌린다. 검사 실패는 5로 나가고 실패한 언어의 파일은 쓰지 않는다.
    """
    return _run_script_stage(
        args, paths, run_localize_stage,
        failure_code=LOCALIZE_FAILURE, langs=_parse_langs(args.lang),
    )


#: `--provider` 값 → 어댑터 팩토리(언어 → 클라이언트). 기본값이 실물인 이유: 페이크가
#: 기본이면 **무음 wav**를 만들어 놓고 나레이션이 생겼다고 착각한 채 다음 단계로 간다.
#: 페이크는 명시적으로 골라야 한다. 목소리는 언어별이다 (ADR-0056 결정 7).
TTS_PROVIDERS = {
    "elevenlabs": lambda lang: ElevenLabsClient(lang=lang),
    "fake": lambda lang: FakeTTSClient(),
}


def _make_tts_factory(args):
    return TTS_PROVIDERS[args.provider]


def _parse_langs(value: str | None) -> list[str] | None:
    """`--lang ko,ja` → 목록. 비우면 None(= 있는 언어 전부)."""
    if not value:
        return None
    langs = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [l for l in langs if l not in LANGUAGES]
    if unknown:
        raise SystemExit(f"오류: 모르는 언어 {unknown} (가능: {', '.join(LANGUAGES)})")
    return langs


def _cmd_tts(args, paths: Paths) -> int:
    """[3] 대본 → narration.{lang}.wav + timing.{lang}.json + scenes.timed.{lang}.json, 언어당 1회.

    입력은 `topics/{slug}/script.md`(ko)와 `script.{ja,en}.md`(있는 것만)다 (ADR-0056).
    run_id는 `runs/*/topic.json`에서 슬러그로 찾는다.

    돈이 드는 단계라 오류를 종료 코드로 구분한다. 고칠 자리가 저마다 다르다:

    - **11** — 키·voice_id·플랜 문제(`TTSNotConfigured`). 고칠 곳은 `.env`이고,
      **호출 전에** 막히므로 과금이 없다 (세 언어 전부 확인한 뒤에야 첫 호출이 나간다)
    - **10** — 어느 언어의 총 길이가 상한을 넘어 그 언어의 실측 파일을 쓰지 않고 멈췄다.
      고칠 곳은 **1부의 대본**이다 (ADR-0017 단방향 경계). 나레이션과 timing은 남는다
    - **9** — 그 밖의 호출·계약 실패 (줄 수 불일치 포함 — 호출 전에 막힌다)
    """
    try:
        result = run_tts_stage(
            args.slug,
            tts=_make_tts_factory(args),
            paths=paths,
            tempo=args.tempo,
            langs=_parse_langs(args.lang),
            force=args.force,
            ffmpeg=args.ffmpeg,
        )
    except TTSNotConfigured as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 11
    except (TTSStageError, TTSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 9

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 10 if result.over_length else 0


def _cmd_scenetable(args, paths: Paths) -> int:
    """[3s] 실측 줄 경계 → 씬 계약 scenes.json (ADR-0049 §5).

    계약 위반은 보고·중단이고 scenes.json을 쓰지 않는다 (ADR-0044) — 세션 출력
    원본은 logs/에 남으므로 사람이 읽고 다시 돌릴지 정한다.
    """
    try:
        run_id = args.run_id or resolve_scenetable_run_id(paths, args.slug)
        result = run_scenetable_stage(
            args.slug,
            llm=_make_client(args, paths.run_dir(run_id) / "logs"),
            paths=paths,
            run_id=run_id,
            force=args.force,
            timeout=args.timeout or SCENETABLE_TIMEOUT,
        )
    except ScenetableStageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 15

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  계약 위반: {error}", file=sys.stderr)
    return 15 if result.errors else 0


def _cmd_refpack(args, paths: Paths) -> int:
    """[4] 씬 계약 → 씬별 실사 참조 사진과 서술 (ADR-0030).

    `[5]`와 선후가 없다 — 둘 다 씬 계약(scenes.json)만 읽는다. 세션은 구독이라
    한계비용이 0이고 사진 내려받기도 무료다. 드는 것은 벽시계뿐이다.

    `--no-download`는 강등 사다리의 **서술** 칸으로 내려서 돈다 — 주소와 라이선스는
    그대로 기록하고 파일만 받지 않는다.
    """
    try:
        run_id = resolve_refpack_run_id(paths, args.slug)
    except RefpackStageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 13

    try:
        result = run_refpack_stage(
            args.slug,
            llm=_make_client(args, paths.run_dir(run_id) / "logs"),
            paths=paths,
            force=args.force,
            fetch=None if args.no_download else urllib_fetch,
            timeout=args.timeout or REFPACK_TIMEOUT,
        )
    except RefpackStageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 13

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  계약 위반: {error}", file=sys.stderr)
    return 13 if result.errors else 0


def _cmd_prompt(args, paths: Paths) -> int:
    """[5] 씬 계약 → 씬별 영상 프롬프트 — 헤드리스 세션 1회 (ADR-0060).

    run_id를 받지 않는다. 2부 산출물은 대본과 같은 run 디렉터리에 놓이고
    그 run_id는 씬 계약(scenes.json)에 적혀 있다 (ADR-0017 "계보는 run_id로 잇는다").

    세션 산출이 샷 서술 계약(promptplan)을 어기면 보고·중단이고 prompts.json을 쓰지
    않는다 (ADR-0044) — 원본은 logs/에 남는다.
    """
    try:
        run_id = resolve_scenetable_run_id(paths, args.slug)
        result = run_prompt_stage(
            args.slug,
            llm=_make_client(args, paths.run_dir(run_id) / "logs"),
            paths=paths,
            force=args.force,
            timeout=args.timeout or PROMPT_TIMEOUT,
            line=args.line,
        )
    except (PromptStageError, ScenetableStageError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 16
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  계약 위반: {error}", file=sys.stderr)
    return 16 if result.errors else 0


#: `--provider` 값 → 영상 어댑터. **기본은 사람이 판정 게이트에서 고른 영상 라인**이다
#: (`judgment/human.json`의 `video_line` → `vocab.json meta.video_line.{line}.provider`,
#: ADR-0059 결정 2). `--provider`를 주면 그것이 이긴다 — 디버깅·페이크용.
#: CLI의 페이크는 FFmpeg `testsrc`로 **재생되는** 클립을 만든다 — 배관을 끝까지 통과시켜 보는
#: 용도라 껍데기 바이트로는 정규화에서 막힌다.
VIDEO_PROVIDERS: dict[str, Callable[[], VideoClient]] = {
    "omni": OmniClient,
    "comfy-h3": ComfyH3Client,
    # `art` 라인의 영상 엔진 (ADR-0070). CLEAN → INFO를 잇는다.
    # **fast다** — ADR-0070 「비용」의 "배치는 relax"는 relax가 동시 3으로 병렬이라는
    # 전제 위에 있었는데, 2026-08-25 실측이 그 전제를 깼다: 계정의 `relaxCoreSize`는 3이지만
    # 실제로 도는 relax 영상 잡은 1개다(`runningCount 1` / MJ `Running Jobs: 1 starting
    # soon`). fast도 동시 1(`coreSize`)이라 병렬성은 같고, 클립당 60초 대 208초로 fast가
    # 3.5배 빠르다 — relax가 사는 값은 시간이 아니라 GPU뿐이다(클립당 1.8분, 18클립이 Pro
    # 잔량의 2%). 사람 결정 2026-08-25.
    "mj-endimage": MidjourneyEndImageClient,
    # `art` 라인의 `info` 씬 전용 (ADR-0072 결정 5) — CLEAN·INFO를 **보간**한다.
    "comfy-h3-fl2v": ComfyH3FirstLastClient,
    "fake": lambda: FakeVideoClient(synth=True),
}


def _resolve_video_provider(args, paths: Paths, run_id: str) -> str:
    """`--provider` 또는 사람의 영상 라인 → 어댑터 이름. 라인이 미구현이면 멈춘다 (조용히 다른
    라인으로 내려가지 않는다 — ADR-0059 결정 2)."""
    if args.provider:
        return str(args.provider)
    # `--line`은 라인 전체를 갈아 끼운다 — 어댑터도 프레임 입력 여부도 그 라인의 것이다.
    # 여기서 판정 파일만 보면 프로바이더와 프레임 해석이 다른 라인에서 오게 된다.
    line = args.line or read_video_line(paths, args.slug or slug_from_run_id(run_id))
    provider = vocab.video_line_meta(line).get("provider")
    if not provider:
        raise VideogenStageError(
            f"영상 라인 '{line}'은 아직 어댑터가 없다 (ADR-0060 전) — judgment/human.json의 "
            "video_line을 바꾸거나 --provider로 지정하라"
        )
    if provider not in VIDEO_PROVIDERS:
        raise VideogenStageError(
            f"vocab.json meta.video_line.{line}.provider={provider!r}가 CLI 어댑터 목록에 없다 "
            f"(있는 것: {', '.join(sorted(VIDEO_PROVIDERS))})"
        )
    return str(provider)


def _make_video_client(args, paths: Paths, run_id: str) -> VideoClient:
    return VIDEO_PROVIDERS[_resolve_video_provider(args, paths, run_id)]()


def _make_info_client(args, paths: Paths, run_id: str) -> VideoClient | None:
    """`info` 씬 전용 엔진 — 라인이 `info_provider`를 말할 때만 (ADR-0072 결정 5).

    `--provider`로 어댑터를 직접 고른 실행에는 붙이지 않는다: 그 플래그는 "이 엔진 하나로
    돌려 보라"는 뜻이고, 씬마다 엔진이 갈리면 그 의도가 깨진다.
    """
    if getattr(args, "provider", None):
        return None
    line = args.line or read_video_line(paths, args.slug or slug_from_run_id(run_id))
    provider = vocab.video_line_meta(line).get("info_provider")
    if not provider:
        return None
    if provider not in VIDEO_PROVIDERS:
        raise VideogenStageError(
            f"vocab.json meta.video_line.{line}.info_provider={provider!r}가 CLI 어댑터 목록에 없다 "
            f"(있는 것: {', '.join(sorted(VIDEO_PROVIDERS))})"
        )
    return VIDEO_PROVIDERS[provider]()


def _cmd_frames(args, paths: Paths) -> int:
    """[6] 씬당 CLEAN(MJ) + INFO(NB2 편집) 두 장 (ADR-0071).

    **프레임을 입력으로 받는 라인에서만 돈다** — 다른 라인이면 그렇다고 말하고 멈춘다.
    `info` 씬마다 편집 호출 1회 + 검수 세션 1회가 붙고, 일반 씬은 CLEAN 한 장으로 끝난다.

    - **17** — 프로바이더 전체 거절(키·플랜·프록시 설정). 남은 씬을 시도하지 않고 멈췄다
    - **8** — 그 밖의 단계 실패 (입력 부재, 프레임을 못 만든 씬)
    """
    if not args.run_id and not args.slug:
        print("오류: --slug나 --run-id 중 하나는 있어야 한다", file=sys.stderr)
        return 8
    try:
        run_id = resolve_frames_run_id(paths, run_id=args.run_id, slug=args.slug)
        llm = (
            _make_client(args, paths.run_dir(run_id) / "logs") if args.review else None
        )
        result = run_frames_stage(
            run_id,
            client=MidjourneyClient(),
            editor=NanoBananaClient(),
            paths=paths,
            llm=llm,
            line=args.line,
            slug=args.slug,
            review=args.review,
            force=args.force,
            jobs=args.jobs,
            image_timeout=args.image_timeout,
            session_timeout=args.timeout or FRAMES_SESSION_TIMEOUT,
        )
    except FramesProviderRefused as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 17
    except (FramesStageError, JudgmentError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 8

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            print(f"  경고: 씬 {outcome.scene_id}: {warning}")
    return 0


def _cmd_videogen(args, paths: Paths) -> int:
    """[7] 씬당 텍스트→영상 클립 1개 + OCR·비전 검수 + 강등 사다리 (ADR-0056).

    **유료 라인(Omni)이면 토픽당 ≈$8이 나가는 단계다** (로컬 라인은 0 — ADR-0059). 종료 코드로 고칠 자리를 가른다:

    - **17** — 프로바이더 전체 거절(키·플랜·파라미터). 남은 씬을 시도하지 않고 멈췄다.
      산 클립과 기록은 남고, 고친 뒤 다시 돌리면 done 씬은 건너뛴다
    - **8** — 그 밖의 단계 실패 (입력 부재, 클립을 못 만든 씬)

    `--review none|ocr|full`: full(기본)은 씬당 비전 세션 1회가 붙는다. 세션 없이 배관만
    보려면 ocr·none. `--provider fake`는 네트워크 없이 껍데기 클립을 만든다.
    """
    if not args.run_id and not args.slug:
        print("오류: --slug나 --run-id 중 하나는 있어야 한다", file=sys.stderr)
        return 8
    try:
        run_id = resolve_videogen_run_id(paths, run_id=args.run_id, slug=args.slug)
        llm = (
            _make_client(args, paths.run_dir(run_id) / "logs")
            if args.review == REVIEW_FULL else None
        )
        result = run_videogen_stage(
            run_id,
            client=_make_video_client(args, paths, run_id),
            info_client=_make_info_client(args, paths, run_id),
            paths=paths,
            llm=llm,
            review=args.review,
            force=args.force,
            jobs=args.jobs,
            review_jobs=args.review_jobs,
            video_timeout=args.video_timeout,
            session_timeout=args.timeout or VIDEOGEN_SESSION_TIMEOUT,
            ffmpeg=args.ffmpeg,
            line=args.line,
            slug=args.slug,
        )
    except ProviderRefused as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 17
    except (VideogenStageError, JudgmentError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 8

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            print(f"  경고: 씬 {outcome.scene_id}: {warning}")
    return 0


def _cmd_ending(args, paths: Paths) -> int:
    """[8] `[4]`가 모아 둔 실사진 → 엔딩 실사 컷 (ADR-0055).

    **새로 수집하지 않는다** — 이미 라이선스를 확인하고 내려받아 둔 재고를 고른다.
    과금 0(구독 세션 1회 + 로컬 인코딩)이고, 쓸 사진이 없으면 `ending.json`을 쓰지
    않은 채 성공으로 끝난다. 그때 `[9]`는 엔딩 없이 지금과 똑같이 돈다 (D-3).
    """
    if not args.run_id and not args.slug:
        print("오류: --slug나 --run-id 중 하나는 있어야 한다", file=sys.stderr)
        return 16

    try:
        run_id = args.run_id or resolve_ending_run_id(paths, args.slug)
        result = run_ending_stage(
            llm=_make_client(args, paths.run_dir(run_id) / "logs"),
            run_id=run_id,
            slug=args.slug,
            paths=paths,
            force=args.force,
            timeout=args.timeout or ENDING_TIMEOUT,
            ffmpeg=args.ffmpeg,
        )
    except EndingStageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 16

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  계약 위반: {error}", file=sys.stderr)
    return 16 if result.errors else 0


def _cmd_assemble(args, paths: Paths) -> int:
    """[9] 클립 + 언어별 실측 → 자막·나레이션이 실린 timeline.{lang}.mp4, 언어당 1회.

    입력이 전부 run 디렉터리에 있어서 `--run-id`만으로 돈다. `--slug`를 주면
    씬 계약(scenes.json)에서 run_id만 읽는다 (ADR-0017). `--lang`을 비우면 실측 파일이
    있는 언어 전부다.
    """
    run_id = resolve_run_id(paths, run_id=args.run_id, slug=args.slug)
    result = run_assemble_stage(
        run_id, paths=paths, langs=_parse_langs(args.lang),
        force=args.force, ffmpeg=args.ffmpeg,
    )
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 0


def _cmd_part1(args, paths: Paths) -> int:
    """[0]+[0f]+[1]+[2]+[2l] 연속 실행 (ADR-0049·0056·0061) — 토픽당 LLM 세션 3회.

    앞 단계가 실패하면 멈춘다. 매체 부적합 반려는 3, 정본 검증 실패는 4, 번안 검사
    실패는 5로 나간다 — 어느 쪽이든 산출물은 topics/{slug}/에 남아 있어 사람이 읽는다.
    """
    topic_result = run_topic_stage(
        args.topic, paths=paths, force=args.force,
        seed_url=getattr(args, "seed_url", None),
    )
    print(topic_result.summary)
    if not topic_result.accepted:
        return 2

    # [0f] 시드 본문 렌더. **실패해도 멈추지 않는다** (D-5) — `[1]`이 WebFetch로
    # 내려간다 (ADR-0061). 단계를 시작조차 못 하는 오류도 여기서는 경고다:
    # 시드 본문은 있으면 좋은 것이지 체인의 조건이 아니다.
    try:
        _report_seedfetch(run_seedfetch_stage(
            topic_result.slug, paths=paths, run_id=topic_result.run_id, force=args.force,
            browser=getattr(args, "browser", None),
        ))
    except SeedfetchStageError as exc:
        print(f"[0f] 건너뛴다 — {exc}")

    client = _make_client(args, topic_result.run_dir / "logs")
    draft_result = run_draft_stage(
        topic_result.slug, llm=client, paths=paths,
        run_id=topic_result.run_id, force=args.force,
    )
    code = _report(draft_result)
    if code:
        return 3 if draft_result.unfit else code

    factcheck_result = run_factcheck_stage(
        topic_result.slug, llm=client, paths=paths,
        run_id=topic_result.run_id, force=args.force,
    )
    code = _report(factcheck_result)
    if code:
        return code

    localize_result = run_localize_stage(
        topic_result.slug, llm=client, paths=paths,
        run_id=topic_result.run_id, force=args.force,
    )
    code = _report(localize_result, LOCALIZE_FAILURE)
    if code:
        return code

    print(
        f"\n대본 완성 — topics/{topic_result.slug}/script.md 와 factcheck.md 를 읽고 "
        "STATUS.md에 go / no-go를 기록하라 (ADR-0009). "
        f"번안({', '.join(TARGET_LANGUAGES)})은 줄 1:1이라 ko의 go가 셋의 go다 (ADR-0056)."
    )
    return 0


def _common_options() -> argparse.ArgumentParser:
    """서브커맨드 앞뒤 어느 위치에서도 받는 공통 옵션.

    default=SUPPRESS라서 지정하지 않으면 네임스페이스를 건드리지 않는다.
    서브파서의 기본값이 앞서 파싱된 전역 값을 덮어쓰는 argparse 동작을 피한다.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help="디버그 로그")
    common.add_argument("--root", type=Path, default=argparse.SUPPRESS,
                        help="프로젝트 루트 (기본: 자동 탐지)")
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                        help="완료된 단계도 다시 실행")
    common.add_argument("--claude-bin", default=argparse.SUPPRESS, help="claude 실행 파일")
    common.add_argument("--model", default=argparse.SUPPRESS,
                        help="헤드리스 세션 모델 (예: sonnet, opus)")
    common.add_argument("--max-retries", type=int, default=argparse.SUPPRESS)
    common.add_argument("--backoff-base", type=int, default=argparse.SUPPRESS)
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="shorts-factory",
        description="지식 쇼츠 파이프라인 (1부: 대본 생산 — ADR-0049)",
        parents=[common],
    )
    # 여기서 set_defaults를 쓰면 안 된다. parents=로 공유된 액션 객체의 default까지
    # 덮어써서 SUPPRESS가 풀리고, 서브파서가 전역 값을 다시 지워버린다.
    # 기본값은 파싱이 끝난 뒤 parse_args()에서 채운다.

    sub = parser.add_subparsers(dest="command", required=True)

    p_topic = sub.add_parser("topic", parents=[common],
                             help="[0] 시드 — 백로그 → 토픽 폴더 + seed.md (LLM 0회)")
    p_topic.add_argument("--topic", default=None, help="소재명 또는 슬러그 (기본: 첫 '후보' 항목)")
    p_topic.add_argument("--seed-url", default=None,
                         help="시드 기사 URL (기본: 백로그의 '시드' 컬럼)")
    p_topic.set_defaults(func=_cmd_topic)

    p_seedfetch = sub.add_parser(
        "seedfetch", parents=[common],
        help="[0f] 시드 URL → seed-body.md (헤드리스 브라우저 렌더, LLM 0회 — ADR-0061)",
    )
    p_seedfetch.add_argument("--slug", required=True)
    p_seedfetch.add_argument("--run-id", default=None)
    p_seedfetch.add_argument(
        "--browser", default=None,
        help="브라우저 실행 파일 경로 (기본: Chrome → Edge 순으로 찾는다. "
             "환경변수 SEEDFETCH_BROWSER도 같은 자리)",
    )
    p_seedfetch.set_defaults(func=_cmd_seedfetch)

    for name, help_text, func in (
        ("draft", "[1] 시드 기사 → 통짜 대본 script.md (ADR-0049)", _cmd_draft),
        ("factcheck", "[2] 대본이 쓴 주장만 검증·정정 → factcheck.md (ADR-0049)", _cmd_factcheck),
    ):
        stage_parser = sub.add_parser(name, parents=[common], help=help_text)
        stage_parser.add_argument("--slug", required=True)
        stage_parser.add_argument("--run-id", default=None)
        stage_parser.set_defaults(func=func)

    p_localize = sub.add_parser(
        "localize", parents=[common],
        help="[2l] 검증 끝난 정본 → script.ja.md + script.en.md, 줄 1:1 (헤드리스 1회 — ADR-0056)",
    )
    p_localize.add_argument("--slug", required=True)
    p_localize.add_argument("--run-id", default=None)
    p_localize.add_argument(
        "--lang", default=None,
        help=f"번안할 언어, 쉼표 구분 (기본: {','.join(TARGET_LANGUAGES)}. "
             "이미 있는 파일은 건드리지 않는다 — 다시 만들려면 지우고 돌린다)",
    )
    p_localize.set_defaults(func=_cmd_localize)

    p_tts = sub.add_parser(
        "tts", parents=[common],
        help="[3] 대본 → narration.wav + 실측 타임스탬프 (2부, 편당 과금)",
    )
    p_tts.add_argument("--slug", required=True)
    p_tts.add_argument(
        "--provider", choices=sorted(TTS_PROVIDERS), default="elevenlabs",
        help="TTS 어댑터 (기본: elevenlabs. 개발·테스트는 fake)",
    )
    p_tts.add_argument(
        "--tempo", type=float, default=DEFAULT_TEMPO,
        help=f"원속 생성 후 적용할 atempo 배속 (기본: {DEFAULT_TEMPO}. specs/04는 1.1~1.2)",
    )
    p_tts.add_argument(
        "--ffmpeg", default="ffmpeg", help="FFmpeg 실행 파일 (기본: PATH의 ffmpeg)",
    )
    p_tts.add_argument(
        "--lang", default=None,
        help="돌릴 언어, 쉼표 구분 (기본: 대본 파일이 있는 언어 전부. ko는 항상 든다)",
    )
    p_tts.set_defaults(func=_cmd_tts)

    p_scenetable = sub.add_parser(
        "scenetable", parents=[common],
        help="[3s] 실측 줄 경계 → 씬 계약 scenes.json (2부, 헤드리스 1회 — ADR-0049)",
    )
    p_scenetable.add_argument("--slug", required=True)
    p_scenetable.add_argument("--run-id", default=None, help="run 디렉터리를 직접 지정")
    p_scenetable.add_argument(
        "--timeout", type=int, default=None,
        help=f"연출표 세션 상한(초) (기본: {SCENETABLE_TIMEOUT})",
    )
    p_scenetable.set_defaults(func=_cmd_scenetable)

    p_refpack = sub.add_parser(
        "refpack", parents=[common],
        help="[4] 씬 계약 → 씬별 실사 참조: 사진 + 서술 (2부, 과금 0 — ADR-0030)",
    )
    p_refpack.add_argument("--slug", required=True)
    p_refpack.add_argument(
        "--no-download", action="store_true",
        help="사진을 내려받지 않고 서술만 쓴다 (강등 사다리의 '서술' 칸)",
    )
    p_refpack.add_argument(
        "--timeout", type=int, default=None,
        help=f"조사 세션 상한(초) (기본: {REFPACK_TIMEOUT})",
    )
    p_refpack.set_defaults(func=_cmd_refpack)

    p_prompt = sub.add_parser("prompt", parents=[common],
                              help="[5] 씬 계약 → 씬별 영상 프롬프트 (2부, 헤드리스 세션 1회 — 스펙 03 골격, ADR-0060)")
    p_prompt.add_argument("--slug", required=True)
    p_prompt.add_argument(
        "--timeout", type=int, default=None,
        help=f"세션 상한(초) (기본 {PROMPT_TIMEOUT})",
    )
    p_prompt.add_argument(
        "--line", default=None,
        help="영상 라인 (기본: judgment/human.json의 video_line — ADR-0059). 라인이 "
             "바꾸는 것은 STYLE 절 유무와 mj_subject뿐이다 (ADR-0070·0071)",
    )
    p_prompt.set_defaults(func=_cmd_prompt)

    p_frames = sub.add_parser(
        "frames", parents=[common],
        help="[6] 씬당 CLEAN(MJ) + INFO(NB2 편집) — 프레임을 입력으로 받는 라인만 (ADR-0071)",
    )
    p_frames.add_argument("--slug", default=None, help="run_id를 씬 계약에서 읽는다")
    p_frames.add_argument("--run-id", default=None)
    p_frames.add_argument(
        "--line", default=None,
        help="영상 라인 (기본: judgment/human.json의 video_line)",
    )
    p_frames.add_argument(
        "--no-review", dest="review", action="store_false", default=True,
        help="INFO 검수 세션을 끈다 (배관 확인용 — 표시가 대상을 가리키는지 아무도 안 본다)",
    )
    p_frames.add_argument(
        "--jobs", type=int, default=None,
        help="동시 워커 수 (기본: 프록시 계정의 coreSize)",
    )
    p_frames.add_argument(
        "--image-timeout", type=int, default=None,
        help="이미지 호출 하나를 기다리는 상한(초) (기본: 어댑터가 정한다 — ADR-0035)",
    )
    p_frames.add_argument(
        "--timeout", type=int, default=None,
        help=f"INFO 검수 세션 상한(초) (기본: {FRAMES_SESSION_TIMEOUT})",
    )
    p_frames.set_defaults(func=_cmd_frames)

    p_videogen = sub.add_parser(
        "videogen", parents=[common],
        help="[7] 씬당 텍스트→영상 클립 + OCR·비전 검수 (2부 — 어댑터는 영상 라인이 정한다, ADR-0056·0059)",
    )
    p_videogen.add_argument("--slug", default=None, help="run_id를 씬 계약에서 읽는다")
    p_videogen.add_argument("--run-id", default=None)
    p_videogen.add_argument(
        "--provider", choices=sorted(VIDEO_PROVIDERS), default=None,
        help="영상 어댑터 (기본: judgment/human.json의 video_line이 정한다 — ADR-0059. "
             "배관 확인은 fake — FFmpeg testsrc 클립, 네트워크 없음)",
    )
    p_videogen.add_argument(
        "--review", choices=REVIEW_MODES, default=REVIEW_FULL,
        help="검수 — full: 끝 프레임 OCR + 씬당 비전 세션 (기본) / ocr: OCR만 / none: 검수 없음",
    )
    p_videogen.add_argument(
        "--jobs", type=int, default=None,
        help="**생성** 동시 수 (기본: 영상 프로바이더에게 묻는다 — 429면 1로 줄인다)",
    )
    p_videogen.add_argument(
        "--review-jobs", type=int, default=None,
        help=(
            "**검수** 동시 수 (기본: %d). 생성과 다른 자원이라 따로 센다 — 검수는 구독 "
            "헤드리스라 영상 엔진 한도가 아니라 플랜 한도를 쓴다 (ADR-0072)"
            % VIDEOGEN_REVIEW_SLOTS
        ),
    )
    p_videogen.add_argument(
        "--video-timeout", type=int, default=None,
        help="영상 호출 하나를 기다리는 상한(초) (기본: 프로바이더가 정한다 — ADR-0035)",
    )
    p_videogen.add_argument(
        "--timeout", type=int, default=None,
        help=f"비전 검수 세션 상한(초) (기본: {VIDEOGEN_SESSION_TIMEOUT})",
    )
    p_videogen.add_argument(
        "--ffmpeg", default="ffmpeg", help="FFmpeg 실행 파일 (기본: PATH의 ffmpeg)",
    )
    p_videogen.add_argument(
        "--line", default=None,
        help="영상 라인 (기본: judgment/human.json의 video_line). 프레임을 입력으로 받는 "
             "라인이면 [6]의 frames.json을 읽어 first/last를 싣는다 (ADR-0071)",
    )
    p_videogen.set_defaults(func=_cmd_videogen)

    p_ending = sub.add_parser(
        "ending", parents=[common],
        help="[8] 실사 참조 → 엔딩 실사 컷 (2부, 과금 0 — ADR-0055. 쓸 사진 없으면 스킵)",
    )
    p_ending.add_argument("--slug", default=None, help="run_id를 씬 계약에서 찾는다")
    p_ending.add_argument("--run-id", default=None, help="run 디렉터리를 직접 지정")
    p_ending.add_argument(
        "--ffmpeg", default="ffmpeg", help="FFmpeg 실행 파일 (기본: PATH의 ffmpeg)",
    )
    p_ending.add_argument(
        "--timeout", type=int, default=None,
        help=f"판정 세션 상한(초) (기본: {ENDING_TIMEOUT})",
    )
    p_ending.set_defaults(func=_cmd_ending)

    p_assemble = sub.add_parser(
        "assemble", parents=[common],
        help="[9] 클립+언어별 실측(+엔딩) → timeline.{lang}.mp4 (2부, 디졸브+자막 번인, 언어당 1회)",
    )
    p_assemble.add_argument("--slug", default=None, help="run_id를 씬 계약에서 읽는다")
    p_assemble.add_argument("--run-id", default=None)
    p_assemble.add_argument(
        "--lang", default=None,
        help="조립할 언어, 쉼표 구분 (기본: 실측 파일이 있는 언어 전부)",
    )
    p_assemble.add_argument(
        "--ffmpeg", default="ffmpeg", help="FFmpeg 실행 파일 (기본: PATH의 ffmpeg)",
    )
    p_assemble.set_defaults(func=_cmd_assemble)

    p_part1 = sub.add_parser("part1", parents=[common],
                             help="[0]+[0f]+[1]+[2]+[2l] 연속 실행 — 토픽당 LLM 세션 3회 (ADR-0049·0056·0061)")
    p_part1.add_argument("--topic", default=None)
    p_part1.add_argument("--browser", default=None,
                         help="[0f]가 쓸 브라우저 실행 파일 경로 (기본: 자동 탐색)")
    p_part1.add_argument("--seed-url", default=None,
                         help="시드 기사 URL (기본: 백로그의 '시드' 컬럼)")
    p_part1.set_defaults(func=_cmd_part1)

    return parser


#: 공통 옵션의 기본값. 액션 default가 SUPPRESS라 파싱 뒤에 채운다.
COMMON_DEFAULTS = {
    "verbose": False,
    "root": None,
    "force": False,
    "claude_bin": "claude",
    "model": None,
    "max_retries": DEFAULT_MAX_RETRIES,
    "backoff_base": DEFAULT_BACKOFF_BASE,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    for key, value in COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()  # basicConfig가 sys.stderr를 붙들기 전에
    args = parse_args(argv)
    _setup_logging(args.verbose)
    paths = Paths(args.root.resolve()) if args.root else Paths.from_env()
    #: API 키를 여기서 한 번 채운다 (ADR-0021). 임포트 시점에 하면 테스트가 서로를
    #: 오염시키고, 어댑터 안에서 하면 단계마다 파일을 다시 읽는다.
    load_dotenv(paths.root / ".env")

    try:
        return args.func(args, paths)
    except (
        TopicStageError, SeedfetchStageError, DraftStageError, FactcheckStageError,
        LocalizeStageError,
        RunNotFound, PromptStageError, AssembleStageError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단됨. 같은 run_id로 다시 실행하면 완료된 단계는 스킵된다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
