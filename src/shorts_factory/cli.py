"""파이프라인 오케스트레이터 CLI.

    python run.py topic     [--topic 소재명]
    python run.py research  --slug SLUG [--only 01-research]
    python run.py script    --slug SLUG           # 팩트시트 → 대본 후보
    python run.py validate  --slug SLUG           # 후보 검증 → 실패 시 재생성 (최대 3회)
    python run.py backfill-scale --slug SLUG      # 확정 대본에 subject_scale만 채움 (ADR-0018)
    python run.py prompt    --slug SLUG           # [2부] 씬 계약 → 씬별 이미지 프롬프트
    python run.py imagegen  --slug SLUG           # [2부] 프롬프트 → images/{scene_id}.png
    python run.py assemble  --slug SLUG           # [2부] 클립+씬 계약 → timeline.mp4
    python run.py package   [--topic 소재명]      # 0a + 0b 연속 실행
    python run.py knowledge reindex               # 소스 카드 인덱스 재생성

ADR-0008에 따라 LLM 단계는 claude 헤드리스 서브프로세스로 실행된다.
`prompt`는 2부 단계이고 LLM도 네트워크도 쓰지 않는다 (순수 룰 변환, ADR-0001).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import DEFAULT_BACKOFF_BASE, DEFAULT_MAX_RETRIES, Paths
from .imagegen.base import ImageClient
from .imagegen.fake import FakeImageClient
from .imagegen.nano_banana import NanoBananaClient
from .knowledge import KnowledgeStore
from .llm.claude_code import ClaudeCodeClient
from .stages.assemble import (
    AssembleStageError,
    resolve_run_id,
    run_assemble_stage,
)
from .stages.backfill_scale import BackfillStageError, run_backfill_scale_stage
from .stages.imagegen import (
    ImagegenStageError,
    StyleAnchorsMissing,
    run_imagegen_stage,
)
from .stages.prompt import PromptStageError, run_prompt_stage
from .stages.research import ResearchStageError, find_run_for_slug, run_research_stage
from .stages.script import ScriptStageError, run_script_stage
from .stages.topic import TopicStageError, run_topic_stage
from .stages.validate import ValidateStageError, run_validate_stage

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
    result = run_topic_stage(args.topic, paths=paths, force=args.force)
    print(result.summary)
    if not result.accepted:
        print(
            "\n백로그에서 미충족 조건을 채우거나 소재를 반려하라 "
            "(specs/06-topic-research.md 소재 4조건).",
            file=sys.stderr,
        )
        return 2
    return 0


def _cmd_research(args, paths: Paths) -> int:
    run_id = args.run_id
    if not run_id:
        run_id, _ = find_run_for_slug(paths, args.slug)

    client = _make_client(args, paths.run_dir(run_id) / "logs")
    result = run_research_stage(
        args.slug, llm=client, paths=paths, run_id=run_id,
        force=args.force, only=args.only,
    )
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    if result.verdict == "fail":
        return 3
    return 0


def _cmd_script(args, paths: Paths) -> int:
    run_id = args.run_id
    if not run_id:
        run_id, _ = find_run_for_slug(paths, args.slug)

    client = _make_client(args, paths.run_dir(run_id) / "logs")
    result = run_script_stage(
        args.slug, llm=client, paths=paths, run_id=run_id, force=args.force,
    )
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  오류: {error}", file=sys.stderr)
    if result.errors:
        print(
            f"\n후보는 {result.candidate_path}에 남겼다. "
            "재생성은 [2. validate] 소관이다 (미구현).",
            file=sys.stderr,
        )
        return 4
    return 0


def _cmd_validate(args, paths: Paths) -> int:
    run_id = args.run_id
    if not run_id:
        run_id, _ = find_run_for_slug(paths, args.slug)

    client = _make_client(args, paths.run_dir(run_id) / "logs")
    result = run_validate_stage(
        args.slug, llm=client, paths=paths, run_id=run_id, force=args.force,
    )
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    for error in result.errors:
        print(f"  오류: {error}", file=sys.stderr)
    if not result.passed:
        print(
            f"\n후보 {len(result.attempts)}개를 모두 남겼다 "
            f"(topics/{args.slug}/05-candidates/). "
            "재생성 상한을 넘었으므로 소재나 팩트시트를 손봐야 한다.",
            file=sys.stderr,
        )
        return 5
    return 0


def _cmd_backfill_scale(args, paths: Paths) -> int:
    """[1x] 확정된 대본에 subject_scale만 채운다 (ADR-0018 일회성 마이그레이션).

    run_id는 대본에 적혀 있지만 이 단계는 run 상태를 남기지 않는다 — 대본 파일 하나를
    제자리에서 고치는 마이그레이션이라 재실행 판단은 필드 유무로 충분하다.
    """
    run_id, _ = find_run_for_slug(paths, args.slug)
    client = _make_client(args, paths.run_dir(run_id) / "logs")
    result = run_backfill_scale_stage(
        args.slug, llm=client, paths=paths, force=args.force,
    )
    print(result.summary)
    return 0


def _cmd_prompt(args, paths: Paths) -> int:
    """[5] 씬 계약 → 씬별 이미지 프롬프트.

    run_id를 받지 않는다. 2부 산출물은 대본과 같은 run 디렉터리에 놓이고
    그 run_id는 06-script.json에 적혀 있다 (ADR-0017 "계보는 run_id로 잇는다").
    """
    result = run_prompt_stage(args.slug, paths=paths, force=args.force)
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 0


#: `--provider` 값 → 어댑터. 기본값이 실물인 이유는 nano_banana.py에 적혀 있다 —
#: 페이크가 기본이면 단색 PNG를 들고 "이미지를 만들었다"고 착각한 채 다음 단계로 간다.
IMAGE_PROVIDERS = {
    "nano-banana": NanoBananaClient,
    "fake": FakeImageClient,
}


def _make_image_client(args) -> ImageClient:
    return IMAGE_PROVIDERS[args.provider]()


def _cmd_imagegen(args, paths: Paths) -> int:
    """[6] 씬별 이미지 프롬프트 → 베이스 이미지.

    입력은 runs/{run_id}/prompts.json 하나다 (ADR-0020). --slug는 run_id를 찾기 위한
    편의일 뿐이라 --run-id를 주면 대본을 열지도 않는다.

    돈이 드는 단계라 오류를 종료 코드로 구분한다 — 6은 생성 실패, 7은 앵커 0장 차단이다.
    """
    try:
        result = run_imagegen_stage(
            images=_make_image_client(args),
            run_id=args.run_id,
            slug=args.slug,
            paths=paths,
            force=args.force,
            allow_missing_anchors=args.allow_missing_anchors,
        )
    except StyleAnchorsMissing as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 7
    except ImagegenStageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 6

    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 0


def _cmd_assemble(args, paths: Paths) -> int:
    """[9] 클립 + 씬 계약 → 자막이 박힌 timeline.mp4.

    입력이 전부 run 디렉터리에 있어서 `--run-id`만으로 돈다. `--slug`를 주면 경계면
    파일(`06-script.json`)에서 run_id만 읽는다 (ADR-0017).
    """
    run_id = resolve_run_id(paths, run_id=args.run_id, slug=args.slug)
    result = run_assemble_stage(
        run_id, paths=paths, force=args.force, ffmpeg=args.ffmpeg,
    )
    print(result.summary)
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return 0


def _cmd_package(args, paths: Paths) -> int:
    topic_result = run_topic_stage(args.topic, paths=paths, force=args.force)
    print(topic_result.summary)
    if not topic_result.accepted:
        return 2

    client = _make_client(args, topic_result.run_dir / "logs")
    research_result = run_research_stage(
        topic_result.slug, llm=client, paths=paths,
        run_id=topic_result.run_id, force=args.force,
    )
    print(research_result.summary)
    for warning in research_result.warnings:
        print(f"  경고: {warning}")
    return 3 if research_result.verdict == "fail" else 0


def _cmd_knowledge(args, paths: Paths) -> int:
    store = KnowledgeStore(paths.knowledge)
    count = store.reindex()
    print(f"소스 카드 {count}건 → {store.index_path}")
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
        description="지식 쇼츠 파이프라인 (1부: 토픽 패키지 생산)",
        parents=[common],
    )
    # 여기서 set_defaults를 쓰면 안 된다. parents=로 공유된 액션 객체의 default까지
    # 덮어써서 SUPPRESS가 풀리고, 서브파서가 전역 값을 다시 지워버린다.
    # 기본값은 파싱이 끝난 뒤 parse_args()에서 채운다.

    sub = parser.add_subparsers(dest="command", required=True)

    p_topic = sub.add_parser("topic", parents=[common],
                             help="[0a] 백로그 → 토픽 패키지 폴더 생성")
    p_topic.add_argument("--topic", default=None, help="소재명 또는 슬러그 (기본: 첫 '후보' 항목)")
    p_topic.set_defaults(func=_cmd_topic)

    p_research = sub.add_parser("research", parents=[common],
                                help="[0b] 조사→검증→비판→팩트시트")
    p_research.add_argument("--slug", required=True)
    p_research.add_argument("--run-id", default=None)
    p_research.add_argument(
        "--only", default=None,
        help="서브스텝 하나만 실행 (01-research | 02-verify | 03-critique | 04-factsheet)",
    )
    p_research.set_defaults(func=_cmd_research)

    p_script = sub.add_parser("script", parents=[common],
                              help="[1] 팩트시트 → 대본 후보")
    p_script.add_argument("--slug", required=True)
    p_script.add_argument("--run-id", default=None)
    p_script.set_defaults(func=_cmd_script)

    p_validate = sub.add_parser("validate", parents=[common],
                                help="[2] 후보 검증 → 실패 사유 피드백 재생성 (최대 3회)")
    p_validate.add_argument("--slug", required=True)
    p_validate.add_argument("--run-id", default=None)
    p_validate.set_defaults(func=_cmd_validate)

    p_backfill = sub.add_parser(
        "backfill-scale", parents=[common],
        help="[1x] 확정 대본에 subject_scale만 채운다 (ADR-0018 마이그레이션)",
    )
    p_backfill.add_argument("--slug", required=True)
    p_backfill.set_defaults(func=_cmd_backfill_scale)

    p_prompt = sub.add_parser("prompt", parents=[common],
                              help="[5] 씬 계약 → 씬별 이미지 프롬프트 (2부, 스펙 03 룰)")
    p_prompt.add_argument("--slug", required=True)
    p_prompt.set_defaults(func=_cmd_prompt)

    p_imagegen = sub.add_parser(
        "imagegen", parents=[common],
        help="[6] 씬별 이미지 프롬프트 → images/{scene_id}.png (2부, 편당 과금)",
    )
    p_imagegen.add_argument("--slug", default=None, help="run_id를 대본에서 찾는다")
    p_imagegen.add_argument("--run-id", default=None, help="run 디렉터리를 직접 지정")
    p_imagegen.add_argument(
        "--provider", choices=sorted(IMAGE_PROVIDERS), default="nano-banana",
        help="이미지 어댑터 (기본: nano-banana. 개발·테스트는 fake)",
    )
    p_imagegen.add_argument(
        "--allow-missing-anchors", action="store_true",
        help="스타일 앵커 0장이어도 진행한다 (ADR-0005 룩 일관성 수단 없이 과금)",
    )
    p_imagegen.set_defaults(func=_cmd_imagegen)

    p_assemble = sub.add_parser(
        "assemble", parents=[common],
        help="[9] 클립+씬 계약 → timeline.mp4 (2부, 디졸브+자막 번인)",
    )
    p_assemble.add_argument("--slug", default=None, help="run_id를 대본에서 읽는다")
    p_assemble.add_argument("--run-id", default=None)
    p_assemble.add_argument(
        "--ffmpeg", default="ffmpeg", help="FFmpeg 실행 파일 (기본: PATH의 ffmpeg)",
    )
    p_assemble.set_defaults(func=_cmd_assemble)

    p_package = sub.add_parser("package", parents=[common], help="[0a]+[0b] 연속 실행")
    p_package.add_argument("--topic", default=None)
    p_package.set_defaults(func=_cmd_package)

    p_knowledge = sub.add_parser("knowledge", parents=[common],
                                 help="소스 카드 라이브러리 (ADR-0012)")
    p_knowledge.add_argument("action", choices=["reindex"],
                             help="reindex: 카드 frontmatter에서 index.md를 다시 만든다")
    p_knowledge.set_defaults(func=_cmd_knowledge)

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

    try:
        return args.func(args, paths)
    except (
        TopicStageError, ResearchStageError, ScriptStageError, ValidateStageError,
        PromptStageError, BackfillStageError, AssembleStageError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단됨. 같은 run_id로 다시 실행하면 완료된 단계는 스킵된다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
