"""[6r. imagereview] — 이미지 판정 → 사분면 교체 / 재생성. ADR-0031 §1~3.

입력은 `[5]` → `[6]`을 실제로 돌려 만든다. 픽스처가 실물 대본이라 씬 수도 프롬프트도
진짜이고, 페이크 어댑터라 돈도 네트워크도 들지 않는다.

확인 대상:
- ADR-0031 §1 — 판정 넷을 세션에 주고, 세션이 **이미지를 열 수 있어야 한다**(`Read`)
- ADR-0031 §2 — `[6]`이 네 장을 다 남기고, 고르는 것은 이 단계다. 하류 계약은 그대로
- ADR-0031 §3 — `redo`는 상한 1회이고 **다시 판정하지 않는다**
- specs/05 D-3 — 후보가 없는 씬은 경고가 아니다. pass/redo만 받는다
- specs/05 D-5 — 재생성이 실패해도 단계 안에서 끝난다
- ADR-0017 — 산출물은 runs/{run_id}/ 아래뿐. topics/는 읽기만 한다
"""

import json

import pytest

from shorts_factory.imagegen.base import ImageGenError
from shorts_factory.imagegen.fake import FakeImageClient
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas.image_review import (
    ImageReviewError,
    candidate_index,
    parse_reviews,
)
from shorts_factory.stages.imagegen import RECORD_FILE as IMAGES_RECORD
from shorts_factory.stages.imagegen import run_imagegen_stage
from shorts_factory.stages.imagereview import (
    PASSED,
    PICKED,
    REDO_FAILED,
    REDO_SKIPPED,
    REGENERATED,
    RECORD_FILE,
    STAGE,
    ImagereviewStageError,
    reviewable_scenes,
    run_imagereview_stage,
)
from shorts_factory.stages.prompt import run_prompt_stage

from conftest import HOOVER, install_script


@pytest.fixture
def prepared(paths):
    """대본 → [5] → [6]까지 돌린 run. `(run_id, 이미지 클라이언트)`를 돌려준다."""

    def _prepare(*, quadrants: int = 4, slug: str = HOOVER):
        install_script(paths, slug)
        run_id = run_prompt_stage(slug, paths=paths, dialect="nb2").run_id
        images = FakeImageClient(quadrants=quadrants)
        run_imagegen_stage(images=images, run_id=run_id, paths=paths)
        return run_id, images

    return _prepare


def scene_ids(paths, run_id) -> list[int]:
    record = json.loads(
        (paths.run_dir(run_id) / IMAGES_RECORD).read_text(encoding="utf-8")
    )
    return [s["scene_id"] for s in reviewable_scenes(record)]


def verdicts(ids, *, default="pass", overrides=None) -> str:
    """세션 응답 하나. 지정하지 않은 씬은 전부 `default`다."""
    overrides = overrides or {}
    reviews = []
    for sid in ids:
        item = {"scene_id": sid, "verdict": default, "pick": None, "reason": "근거"}
        item.update(overrides.get(sid, {}))
        reviews.append(item)
    return json.dumps({"reviews": reviews}, ensure_ascii=False)


def read_record(paths, run_id) -> dict:
    return json.loads(
        (paths.run_dir(run_id) / RECORD_FILE).read_text(encoding="utf-8")
    )


# --- [6]이 네 장을 남긴다 (ADR-0031 §2) --------------------------------------


def test_imagegen_keeps_every_quadrant_and_still_writes_one_image(paths, prepared):
    """하류 계약은 그대로다 — `[7]`은 여전히 `images/{scene_id}.png` 한 장만 안다."""
    run_id, _ = prepared()
    run_dir = paths.run_dir(run_id)
    record = json.loads((run_dir / IMAGES_RECORD).read_text(encoding="utf-8"))

    first = record["scenes"][0]
    assert first["candidates"] == [
        f"images/_cand/{first['scene_id']}/q{i}.png" for i in range(4)
    ]
    for relative in first["candidates"]:
        assert (run_dir / relative).exists()

    # 본 파일은 q0이다. 고르는 것은 [6]이 아니다.
    q0 = (run_dir / first["candidates"][0]).read_bytes()
    assert (run_dir / first["file"]).read_bytes() == q0


def test_a_provider_without_candidates_records_an_empty_list(paths, prepared):
    """후보라는 개념이 없는 프로바이더가 정상이다 (specs/05 D-3)."""
    run_id, _ = prepared(quadrants=0)
    record = json.loads(
        (paths.run_dir(run_id) / IMAGES_RECORD).read_text(encoding="utf-8")
    )
    assert all(s["candidates"] == [] for s in record["scenes"])
    assert not (paths.run_dir(run_id) / "images" / "_cand").exists()


# --- 세션에 무엇을 주는가 (ADR-0031 §1) --------------------------------------


def test_the_session_gets_read_and_the_images_directory(paths, prepared):
    """G1 — 판정은 **파일을 열어서** 한다. 도구도 경로도 없으면 짐작만 남는다."""
    run_id, _ = prepared()
    llm = FakeLLMClient([verdicts(scene_ids(paths, run_id))])
    run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)

    call = llm.calls[0]
    assert call["allowed_tools"] == ("Read",)
    assert call["add_dirs"] == (paths.run_dir(run_id) / "images",)


def test_the_prompt_carries_the_picture_fields_but_never_the_script(paths, prepared):
    """이미지 판정은 그림 필드만 본다 (ADR-0038).

    옛 계약은 자막(`text`)을 갈라 줄 근거로 줬는데 **그것은 대본 문장에 대한 의존**이고
    기각 사유로는 쓰지 못하게 막아 둔 값이었다. 판정에 필요한 것은 `visual_goal`이
    이미 말하고 있다.
    """
    run_id, _ = prepared()
    llm = FakeLLMClient([verdicts(scene_ids(paths, run_id))])
    run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)

    prompt = llm.calls[0]["prompt"]
    script = json.loads(
        (paths.topic_dir(HOOVER) / "06-script.json").read_text(encoding="utf-8")
    )
    first = script["scenes"][0]
    assert first["subject"] in prompt
    assert first["visual_goal"] in prompt
    assert first["text"] not in prompt
    # 씬 블록에 자막 항목이 없다. (본문이 "자막은 보지 않는다"고 설명하기는 한다)
    assert "- 자막:" not in prompt
    assert "q0: images/_cand/1/q0.png" in prompt


def test_scenes_without_candidates_are_told_so(paths, prepared):
    run_id, _ = prepared(quadrants=0)
    llm = FakeLLMClient([verdicts(scene_ids(paths, run_id))])
    run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)
    assert "후보: 없음" in llm.calls[0]["prompt"]


# --- pass / pick (ADR-0031 §2) ------------------------------------------------


def test_pass_leaves_the_image_alone(paths, prepared):
    run_id, _ = prepared()
    run_dir = paths.run_dir(run_id)
    before = (run_dir / "images" / "1.png").read_bytes()

    llm = FakeLLMClient([verdicts(scene_ids(paths, run_id))])
    result = run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)

    assert (run_dir / "images" / "1.png").read_bytes() == before
    assert result.count(PASSED) == result.scene_count


def test_pick_swaps_the_quadrant_in_place_without_buying_anything(paths, prepared):
    """**이미 산 것을 고르는 일이라 과금이 0이다.** 호출 수가 늘면 안 된다."""
    run_id, images = prepared()
    run_dir = paths.run_dir(run_id)
    ids = scene_ids(paths, run_id)
    calls_before = len(images.calls)

    llm = FakeLLMClient([
        verdicts(ids, overrides={
            ids[0]: {"verdict": "pick", "pick": "q2", "reason": "q2가 구조를 보여준다"}
        })
    ])
    result = run_imagereview_stage(
        llm=llm, images=images, run_id=run_id, paths=paths
    )

    expected = (run_dir / "images" / "_cand" / str(ids[0]) / "q2.png").read_bytes()
    assert (run_dir / "images" / f"{ids[0]}.png").read_bytes() == expected
    assert len(images.calls) == calls_before  # 산 것이 없다
    assert result.count(PICKED) == 1

    entry = next(s for s in read_record(paths, run_id)["scenes"] if s["scene_id"] == ids[0])
    assert entry["outcome"] == PICKED
    assert entry["source"] == f"images/_cand/{ids[0]}/q2.png"


def test_pick_on_a_scene_without_candidates_is_refused(paths, prepared):
    """바꿔 낄 파일이 없다. 조용히 통과시키면 판정이 있었다고 착각하게 된다."""
    run_id, _ = prepared(quadrants=0)
    ids = scene_ids(paths, run_id)
    llm = FakeLLMClient([
        verdicts(ids, overrides={ids[0]: {"verdict": "pick", "pick": "q1"}})
    ])
    with pytest.raises(ImagereviewStageError, match="후보가 없는데"):
        run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)


# --- redo (ADR-0031 §3) -------------------------------------------------------


def test_redo_buys_the_scene_again_exactly_once_and_does_not_rejudge(paths, prepared):
    """판정 → 재생성 → 판정이 되면 끝나는 조건이 사람 말고는 없다."""
    run_id, images = prepared()
    ids = scene_ids(paths, run_id)
    calls_before = len(images.calls)

    llm = FakeLLMClient([
        verdicts(ids, overrides={
            ids[1]: {"verdict": "redo", "reason": "네 장 모두 대상이 아니다"}
        })
    ])
    result = run_imagereview_stage(
        llm=llm, images=images, run_id=run_id, paths=paths
    )

    assert images.attempts_for(ids[1]) == 2  # [6]에서 1 + 여기서 1
    assert len(images.calls) == calls_before + 1  # 다른 씬은 안 샀다
    assert len(llm.calls) == 1  # 세션은 한 번뿐 — 다시 판정하지 않는다
    assert result.count(REGENERATED) == 1


def test_redo_without_a_provider_keeps_the_current_image(paths, prepared):
    """실패는 단계 안에서 끝난다 (specs/05 D-5)."""
    run_id, _ = prepared()
    run_dir = paths.run_dir(run_id)
    ids = scene_ids(paths, run_id)
    before = (run_dir / "images" / f"{ids[0]}.png").read_bytes()

    llm = FakeLLMClient([
        verdicts(ids, overrides={ids[0]: {"verdict": "redo", "reason": "대상이 다르다"}})
    ])
    result = run_imagereview_stage(llm=llm, images=None, run_id=run_id, paths=paths)

    assert (run_dir / "images" / f"{ids[0]}.png").read_bytes() == before
    assert result.count(REDO_SKIPPED) == 1
    assert any("프로바이더가 없다" in w for w in result.warnings)


def test_a_failed_redo_is_recorded_and_the_stage_finishes(paths, prepared):
    run_id, _ = prepared()
    ids = scene_ids(paths, run_id)
    broken = FakeImageClient(
        quadrants=4, error=lambda sid, attempt: ImageGenError("프록시가 죽었다")
    )
    broken.fail_scenes = {ids[0]: 9}

    llm = FakeLLMClient([
        verdicts(ids, overrides={ids[0]: {"verdict": "redo", "reason": "대상이 다르다"}})
    ])
    result = run_imagereview_stage(
        llm=llm, images=broken, run_id=run_id, paths=paths
    )

    assert result.count(REDO_FAILED) == 1
    entry = next(s for s in result.scenes if s["scene_id"] == ids[0])
    assert "프록시가 죽었다" in entry["error"]


# --- 기록과 재실행 ------------------------------------------------------------


def test_the_record_lands_in_the_run_directory_only(paths, prepared):
    """ADR-0017 — 2부는 topics/를 쓰지 않는다."""
    run_id, _ = prepared()
    llm = FakeLLMClient([verdicts(scene_ids(paths, run_id))])
    run_imagereview_stage(llm=llm, run_id=run_id, paths=paths)

    document = read_record(paths, run_id)
    assert document["run_id"] == run_id
    assert document["stage"] == STAGE
    assert len(document["scenes"]) == len(scene_ids(paths, run_id))
    assert sorted(p.name for p in paths.topic_dir(HOOVER).iterdir()) == [
        "06-script.json"
    ]


def test_a_finished_stage_is_skipped_without_calling_the_session(paths, prepared):
    run_id, _ = prepared()
    ids = scene_ids(paths, run_id)
    run_imagereview_stage(llm=FakeLLMClient([verdicts(ids)]), run_id=run_id, paths=paths)

    empty = FakeLLMClient([])
    result = run_imagereview_stage(llm=empty, run_id=run_id, paths=paths)

    assert result.skipped and empty.calls == []
    assert len(result.scenes) == len(ids)


def test_force_runs_it_again(paths, prepared):
    run_id, _ = prepared()
    ids = scene_ids(paths, run_id)
    run_imagereview_stage(llm=FakeLLMClient([verdicts(ids)]), run_id=run_id, paths=paths)

    again = FakeLLMClient([verdicts(ids)])
    run_imagereview_stage(llm=again, run_id=run_id, paths=paths, force=True)
    assert len(again.calls) == 1


def test_it_refuses_to_run_before_imagegen(paths):
    install_script(paths, HOOVER)
    run_id = run_prompt_stage(HOOVER, paths=paths, dialect="nb2").run_id
    with pytest.raises(ImagereviewStageError, match="images.json"):
        run_imagereview_stage(llm=FakeLLMClient([]), run_id=run_id, paths=paths)


# --- 판정 파싱 (schemas/image_review.py) --------------------------------------


def test_a_missing_scene_fails_rather_than_passing_silently():
    payload = {"reviews": [{"scene_id": 1, "verdict": "pass", "reason": "좋다"}]}
    with pytest.raises(ImageReviewError, match="판정이 빠진 씬"):
        parse_reviews(payload, {1: [], 2: []})


def test_an_unknown_verdict_is_refused():
    payload = {"reviews": [{"scene_id": 1, "verdict": "maybe", "reason": "글쎄"}]}
    with pytest.raises(ImageReviewError, match="verdict"):
        parse_reviews(payload, {1: []})


def test_an_out_of_range_quadrant_is_refused():
    payload = {
        "reviews": [{"scene_id": 1, "verdict": "pick", "pick": "q7", "reason": "저것"}]
    }
    with pytest.raises(ImageReviewError, match="q7이 없다"):
        parse_reviews(payload, {1: ["a", "b"]})


def test_a_pick_on_a_pass_verdict_is_refused():
    """둘 다 오면 무엇을 하려던 것인지 알 수 없다."""
    payload = {
        "reviews": [{"scene_id": 1, "verdict": "pass", "pick": "q1", "reason": "좋다"}]
    }
    with pytest.raises(ImageReviewError, match="pick="):
        parse_reviews(payload, {1: ["a", "b"]})


def test_an_empty_reason_is_refused():
    """기각 사유 없는 판정은 사람이 감사할 수 없다 (ADR-0031 G4)."""
    payload = {"reviews": [{"scene_id": 1, "verdict": "pass", "reason": "  "}]}
    with pytest.raises(ImageReviewError, match="reason"):
        parse_reviews(payload, {1: []})


def test_candidate_names_must_look_like_q_n():
    assert candidate_index("q3") == 3
    with pytest.raises(ImageReviewError):
        candidate_index("두 번째")
