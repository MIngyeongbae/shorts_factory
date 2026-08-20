"""[1b] score — 후보 채점 → 선발. ADR-0040.

이 단계가 지는 것은 **어느 후보가 시청자를 잡는가** 하나다. 그래서 확인하는 것도 그 하나에
붙는 계약들이다.

- ADR-0040 — 임계값은 값 파일에 있고, **캘리브레이션 전이면 반려하지 않는다**
- ADR-0040 — 채점 세션에 **팩트시트를 주지 않는다**. 사실 검증은 다른 단계 몫이다
- specs/05:24 — **비판 반영 채점**. `03-critique.md`가 프롬프트에 실린다
- specs/05:126 — 전 후보가 기준 미달이면 선발하지 않는다
- specs/05 D-3 — 선택적 입력(구성안·비판)이 없어도 단계는 돈다
- 총점은 세션이 더한 값이라 **합을 다시 검산한다** — 틀리면 채점 전체를 못 믿는다
"""

import json
from datetime import date

import pytest

from conftest import HOOVER, load_fixture, load_script
from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas import score as score_schema
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.score import (
    SCORE_FILE,
    SCRIPT_FILE,
    format_candidates,
    run_score_stage,
)
from shorts_factory.stages.topic import run_topic_stage

TODAY = date(2026, 8, 19)
TOPIC = "후버댐 콘크리트 냉각"
AXES = ("hook_strength", "comprehension", "standalone")


def scored(name: str, *values: int, reflected=("비판이 지목한 훅 약점",)) -> dict:
    """후보 하나의 채점 블록. 총점은 축 합이다."""
    return {
        "candidate": name,
        "axes": {
            axis: {"score": v, "why": f"{axis} 근거"} for axis, v in zip(AXES, values)
        },
        "total": sum(values),
        "critique_reflected": list(reflected),
    }


def response(*blocks: dict, chosen: str | None = None) -> str:
    payload = {
        "topic": TOPIC,
        "candidates": list(blocks),
        "chosen": chosen if chosen is not None else blocks[0]["candidate"],
        "why_chosen": "훅이 첫 3초에 통념과 부딪힌다. 진 후보는 규격 나열로 시작한다",
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def prepared(paths):
    """[0b]까지 끝나고 후보 2개가 놓인 토픽 패키지."""
    backlog = paths.root / "topics" / "backlog.md"
    backlog.write_text(
        backlog.read_text(encoding="utf-8")
        + f"| {TOPIC} | ✅ | ✅ | ✅ | ✅ | 개척국 기술보고서 | 후보 |\n",
        encoding="utf-8",
    )
    run_topic_stage(TOPIC, paths=paths, today=TODAY)
    run_research_stage(
        HOOVER,
        llm=FakeLLMClient(
            [
                "# 조사",
                "# 검증",
                "# 비판\n\n권고: 조건부 진행. 훅 후보가 전부 약하다",
                json.dumps(load_fixture("factsheet_hoover.json"), ensure_ascii=False),
            ]
        ),
        paths=paths,
    )
    topic_dir = paths.topic_dir(HOOVER)
    base = load_script(HOOVER)
    write_text(topic_dir / "05-candidates" / "01.json", dump_json(base))

    second = json.loads(json.dumps(base))
    second["scenes"][0]["text"] = "콘크리트는 마르면서 굳는 게 아닙니다."
    write_text(topic_dir / "05-candidates" / "02.json", dump_json(second))

    write_text(
        topic_dir / "07-outline.json",
        dump_json({"topic": TOPIC, "chosen_hook": 0, "why_chosen": "통짜가 아니다"}),
    )
    return paths, topic_dir


def test_선발본이_06script로_복사된다(prepared):
    paths, topic_dir = prepared
    llm = FakeLLMClient(
        [response(scored("01.json", 4, 4, 4), scored("02.json", 3, 3, 3))]
    )

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.valid, result.errors
    assert result.verdict == "pass"
    assert result.score["chosen"] == "01.json"
    saved = json.loads((topic_dir / SCRIPT_FILE).read_text(encoding="utf-8"))
    candidate = json.loads(
        (topic_dir / "05-candidates" / "01.json").read_text(encoding="utf-8")
    )
    assert saved["scenes"] == candidate["scenes"]
    assert (topic_dir / SCORE_FILE).exists()


def test_채점_세션에_팩트시트를_주지_않는다(prepared):
    """ADR-0040 — 채점은 사실 검증이 아니다."""
    paths, topic_dir = prepared
    llm = FakeLLMClient([response(scored("01.json", 4, 4, 4), scored("02.json", 3, 3, 3))])

    run_score_stage(HOOVER, llm=llm, paths=paths)

    prompt = llm.calls[0]["prompt"]
    factsheet = json.loads((topic_dir / "04-factsheet.json").read_text(encoding="utf-8"))
    assert factsheet["facts"][0]["claim"] not in prompt
    assert "04-factsheet" not in prompt


def test_비판이_프롬프트에_실린다(prepared):
    """specs/05:24 — 비판 반영 채점."""
    paths, _ = prepared
    llm = FakeLLMClient([response(scored("01.json", 4, 4, 4), scored("02.json", 3, 3, 3))])

    run_score_stage(HOOVER, llm=llm, paths=paths)

    assert "훅 후보가 전부 약하다" in llm.calls[0]["prompt"]


def test_구성안과_비판이_없어도_돈다(prepared):
    """specs/05 D-3 — 선택적 입력의 부재는 경고가 아니다."""
    paths, topic_dir = prepared
    (topic_dir / "07-outline.json").unlink()
    (topic_dir / "03-critique.md").unlink()
    llm = FakeLLMClient([response(scored("01.json", 4, 4, 4), scored("02.json", 3, 3, 3))])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.valid, result.errors
    assert result.verdict == "pass"


def test_총점이_축_합과_다르면_반려된다(prepared):
    paths, topic_dir = prepared
    block = scored("01.json", 4, 4, 4)
    block["total"] = 15
    llm = FakeLLMClient([response(block)])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert not result.valid
    assert any("축 합" in e for e in result.errors)
    assert not (topic_dir / SCRIPT_FILE).exists()


def test_없는_후보를_채점하면_반려된다(prepared):
    paths, topic_dir = prepared
    llm = FakeLLMClient([response(scored("99.json", 5, 5, 5))])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert not result.valid
    assert any("존재하지 않는 후보" in e for e in result.errors)
    assert not (topic_dir / SCRIPT_FILE).exists()


def test_캘리브레이션_전에는_미달_반려를_하지_않는다(prepared, monkeypatch):
    """ADR-0040 — 근거 없는 수로 반려선을 긋지 않는다."""
    paths, topic_dir = prepared
    monkeypatch.setattr(score_schema, "thresholds", lambda: (None, None))
    llm = FakeLLMClient([response(scored("01.json", 0, 0, 0), scored("02.json", 1, 0, 0))])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.verdict == "pass"
    assert (topic_dir / SCRIPT_FILE).exists()
    assert any("min_total이 비어 있다" in w for w in result.warnings)


def test_전_후보_미달이면_선발하지_않는다(prepared, monkeypatch):
    """specs/05:126 — 전 후보가 기준 미달이면 주제를 반려하고 리포트한다."""
    paths, topic_dir = prepared
    monkeypatch.setattr(score_schema, "thresholds", lambda: (12, 3))
    llm = FakeLLMClient([response(scored("01.json", 2, 2, 2), scored("02.json", 3, 3, 3))])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.valid, result.errors
    assert result.verdict == "reject"
    assert result.score["chosen"] is None
    assert not (topic_dir / SCRIPT_FILE).exists()
    assert any("기준 미달" in w for w in result.warnings)


def test_세션이_고른_후보가_미달이면_통과한_최고점으로_내린다(prepared, monkeypatch):
    paths, topic_dir = prepared
    monkeypatch.setattr(score_schema, "thresholds", lambda: (12, 3))
    llm = FakeLLMClient(
        [
            response(
                scored("01.json", 2, 2, 2),
                scored("02.json", 4, 4, 4),
                chosen="01.json",
            )
        ]
    )

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.verdict == "pass"
    assert result.score["chosen"] == "02.json"
    assert "최고점" in result.score["notes"]
    assert (topic_dir / SCRIPT_FILE).exists()


def test_후보가_없으면_실행되지_않는다(prepared):
    paths, topic_dir = prepared
    for path in (topic_dir / "05-candidates").glob("*.json"):
        path.unlink()

    with pytest.raises(Exception) as exc:
        run_score_stage(HOOVER, llm=FakeLLMClient([]), paths=paths)
    assert "후보가 없다" in str(exc.value)


def test_후보_포맷에_연출_필드가_실리지_않는다():
    """채점 축과 무관한 필드는 세션에 주지 않는다. visual_goal만 남긴다."""
    doc = load_script(HOOVER)
    text = format_candidates({"01.json": doc})

    assert doc["scenes"][0]["text"] in text
    assert doc["scenes"][0]["visual_goal"] in text
    assert doc["scenes"][0]["camera"] not in text
    assert "subject_scale" not in text


def test_깨진_JSON은_다시_부른다(prepared):
    """실전 첫 호출에서 세션이 `axes`의 닫는 중괄호를 빠뜨렸다. 생성 슬립은 재시도로 붙는다."""
    paths, topic_dir = prepared
    broken = '{"topic":"t","candidates":[{"candidate":"01.json","axes":{"x":{"score":1,"why":"a"}'
    llm = FakeLLMClient([broken, response(scored("01.json", 4, 4, 4))])

    result = run_score_stage(HOOVER, llm=llm, paths=paths)

    assert result.valid, result.errors
    assert len(llm.calls) == 2
    assert "JSON이 깨졌다" in llm.calls[1]["prompt"], "실패 사유가 피드백된다"
    assert (topic_dir / SCRIPT_FILE).exists()


def test_계속_깨지면_상한에서_멈춘다(prepared):
    paths, topic_dir = prepared
    llm = FakeLLMClient(["깨진 응답"] * 5)

    with pytest.raises(Exception):
        run_score_stage(HOOVER, llm=llm, paths=paths)

    assert len(llm.calls) == 3, "MAX_SCORE_ATTEMPTS를 넘지 않는다"
    assert not (topic_dir / SCRIPT_FILE).exists()


def test_기존_채점이_있으면_스킵한다(prepared):
    paths, topic_dir = prepared
    llm = FakeLLMClient([response(scored("01.json", 4, 4, 4), scored("02.json", 3, 3, 3))])
    run_score_stage(HOOVER, llm=llm, paths=paths)

    again = run_score_stage(HOOVER, llm=FakeLLMClient([]), paths=paths)

    assert again.skipped
    assert again.score["chosen"] == "01.json"
