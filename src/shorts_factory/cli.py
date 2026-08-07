"""파이프라인 오케스트레이터 CLI.

    python run.py topic     [--topic 소재명]
    python run.py research  --slug SLUG [--only 01-research]
    python run.py package   [--topic 소재명]      # 0a + 0b 연속 실행
    python run.py knowledge reindex               # 소스 카드 인덱스 재생성

ADR-0008에 따라 LLM 단계는 claude 헤드리스 서브프로세스로 실행된다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import DEFAULT_BACKOFF_BASE, DEFAULT_MAX_RETRIES, Paths
from .knowledge import KnowledgeStore
from .llm.claude_code import ClaudeCodeClient
from .stages.research import ResearchStageError, find_run_for_slug, run_research_stage
from .stages.topic import TopicStageError, run_topic_stage

log = logging.getLogger("shorts_factory")


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
    args = parse_args(argv)
    _setup_logging(args.verbose)
    paths = Paths(args.root.resolve()) if args.root else Paths.from_env()

    try:
        return args.func(args, paths)
    except (TopicStageError, ResearchStageError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단됨. 같은 run_id로 다시 실행하면 완료된 단계는 스킵된다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
