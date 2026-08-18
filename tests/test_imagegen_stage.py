"""[6. imagegen] — prompts.json → images/{scene_id}.png.

입력은 `[5]`를 실제로 돌려 만든다. 픽스처가 실물 대본 2편이라(ADR-0017) 씬 수도
프롬프트도 진짜다. `[5]`는 순수 룰 변환이라 돈도 네트워크도 들지 않는다.

확인 대상:
- specs/05 실패 정책 — 씬당 1회 재시도, 2회 실패 시 인접 씬 이미지 폴백, 계속 진행
- ADR-0017 경계 — 산출물은 runs/{run_id}/ 아래뿐, topics/는 손대지 않는다
- ADR-0019 — 씬당 호출 1회. 편당 베이스 호출 = 씬 수
- ADR-0020 — `framing_reuse_of`는 캐시 힌트가 아니다
- ADR-0005 — 스타일 앵커가 0장이면 과금 프로바이더는 호출 전에 막힌다
- 돈 — 이미 산 이미지는 다시 사지 않는다
"""

import json
import struct
from pathlib import Path

import pytest

from shorts_factory.cli import parse_args as parse
from shorts_factory.imagegen.fake import FakeImageClient
from shorts_factory.imagegen.nano_banana import NanoBananaClient
from shorts_factory.stages.imagegen import (
    CACHED,
    FALLBACK,
    GENERATED,
    IMAGES_DIR,
    MAX_ATTEMPTS,
    PROMPTS_FILE,
    RECORD_FILE,
    STAGE,
    DialectMismatch,
    ImagegenStageError,
    StyleAnchorsMissing,
    resolve_run_id,
    run_imagegen_stage,
)
from shorts_factory.stages.prompt import run_prompt_stage

from conftest import HOOVER, PISA, install_script

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PaidFake(FakeImageClient):
    """과금 프로바이더 흉내 — 앵커 없이는 못 돈다는 점만 다르다 (ADR-0005)."""

    requires_style_anchors = True
    name = "paid-fake"


@pytest.fixture
def prepared(paths):
    """격리된 루트에 대본 → [5] 실행 → prompts.json. run_id를 돌려준다.

    방언 기본값은 `nb2`다 — 이 파일의 과금 어댑터가 `NanoBananaClient` 하나이고,
    페이크는 방언을 가리지 않는다 (ADR-0027). MJ 경로는 전용 테스트에서 본다.
    """

    def _prepare(slug: str = HOOVER, *, dialect: str = "nb2") -> str:
        install_script(paths, slug)
        return run_prompt_stage(slug, paths=paths, dialect=dialect).run_id

    return _prepare


def put_anchor(paths, name: str = "anchor-01.png") -> Path:
    path = paths.root / "assets" / "style_anchors" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_MAGIC + b"anchor")
    return path


def read_record(paths, run_id: str) -> dict:
    return json.loads(
        (paths.run_dir(run_id) / RECORD_FILE).read_text(encoding="utf-8")
    )


def scene_record(result, scene_id: int) -> dict:
    return next(s for s in result.scenes if s["scene_id"] == scene_id)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


# --- 씬 하나에 이미지 하나 ---------------------------------------------------


@pytest.mark.parametrize("slug", (PISA, HOOVER))
def test_every_scene_gets_exactly_one_image(paths, prepared, slug):
    """ADR-0019 — 2-pass가 없다. 편당 베이스 호출 = 씬 수다."""
    run_id = prepared(slug)
    prompts = json.loads(
        (paths.run_dir(run_id) / PROMPTS_FILE).read_text(encoding="utf-8")
    )
    client = FakeImageClient()

    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    scene_ids = [s["scene_id"] for s in prompts["scenes"]]
    assert client.scene_calls == scene_ids
    assert result.calls == len(scene_ids)
    for scene_id in scene_ids:
        assert (paths.run_dir(run_id) / IMAGES_DIR / f"{scene_id}.png").exists()


def test_images_are_real_png_files(paths, prepared):
    run_id = prepared()
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)

    for path in (paths.run_dir(run_id) / IMAGES_DIR).glob("*.png"):
        data = path.read_bytes()
        assert data.startswith(PNG_MAGIC)
        width, height = struct.unpack(">II", data[16:24])
        assert width * 16 == height * 9  # 9:16 (specs/03)


def test_same_framing_is_not_treated_as_an_image_cache(paths, prepared):
    """ADR-0020 — 구도만 같고 subject는 다르다. 캐시 키로 쓰면 다른 피사체가 같은 그림이 된다."""
    run_id = prepared(PISA)
    prompts = json.loads(
        (paths.run_dir(run_id) / PROMPTS_FILE).read_text(encoding="utf-8")
    )
    seen: dict[str, int] = {}
    pair = None
    for entry in prompts["scenes"]:
        token = entry["framing"]
        if token in seen:
            pair = (seen[token], entry["scene_id"])
            break
        seen[token] = entry["scene_id"]
    assert pair, "같은 구도를 두 번 쓰는 씬이 없어 이 회귀를 볼 수 없다"

    first, second = pair
    client = FakeImageClient()

    run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    images = paths.run_dir(run_id) / IMAGES_DIR
    assert client.attempts_for(second) == 1  # 건너뛰지 않았다
    assert (images / f"{second}.png").read_bytes() != (
        images / f"{first}.png"
    ).read_bytes()


# --- 경계 (ADR-0017) ---------------------------------------------------------


def test_topics_tree_is_untouched(paths, prepared):
    run_id = prepared()
    before = snapshot(paths.topics)
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    assert snapshot(paths.topics) == before


def test_run_id_alone_is_enough(paths, prepared):
    """[6]의 입력은 prompts.json 하나다. 대본이 사라져도 돈다 (ADR-0020)."""
    run_id = prepared()
    for path in sorted(paths.topics.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    result = run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    assert result.passed


def test_slug_resolves_the_run_id_from_the_boundary_file(paths, prepared):
    run_id = prepared()
    assert resolve_run_id(paths, HOOVER) == run_id

    result = run_imagegen_stage(images=FakeImageClient(), slug=HOOVER, paths=paths)
    assert result.run_id == run_id


def test_run_id_only_leaves_no_null_slug_in_the_state(paths, prepared):
    run_id = prepared()
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["slug"] == HOOVER  # [5]가 심어 둔 값이 그대로다


def test_neither_run_id_nor_slug_is_a_clear_error(paths):
    with pytest.raises(ImagegenStageError, match="run_id"):
        run_imagegen_stage(images=FakeImageClient(), paths=paths)


def test_a_skipped_run_reports_zero_cost(paths, prepared):
    run_id = prepared()
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    again = run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)

    assert again.skipped and again.calls == 0
    assert "호출 0회" in again.summary


def test_missing_prompts_points_at_stage_five(paths):
    with pytest.raises(ImagegenStageError, match=r"\[5\. prompt\]"):
        run_imagegen_stage(
            images=FakeImageClient(), run_id="20260810-없는-run", paths=paths
        )


def test_broken_prompts_contract_stops_before_spending(paths, prepared):
    run_id = prepared()
    prompts_path = paths.run_dir(run_id) / PROMPTS_FILE
    broken = json.loads(prompts_path.read_text(encoding="utf-8"))
    broken["scenes"][0]["framing"] = "banana_wide"
    prompts_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    client = FakeImageClient()

    with pytest.raises(ImagegenStageError, match="계약"):
        run_imagegen_stage(images=client, run_id=run_id, paths=paths)
    assert client.calls == []


def test_duplicate_scene_ids_would_overwrite_each_other(paths, prepared):
    run_id = prepared()
    prompts_path = paths.run_dir(run_id) / PROMPTS_FILE
    data = json.loads(prompts_path.read_text(encoding="utf-8"))
    data["scenes"][1]["scene_id"] = data["scenes"][0]["scene_id"]
    prompts_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ImagegenStageError, match="scene_id"):
        run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)


# --- 실패 정책 (specs/05) ----------------------------------------------------


def test_one_retry_is_enough_when_the_second_call_succeeds(paths, prepared):
    run_id = prepared()
    client = FakeImageClient(fail_scenes={5: 1})

    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    record = scene_record(result, 5)
    assert record["status"] == GENERATED
    assert record["attempts"] == 2
    assert len(record["errors"]) == 1
    assert client.attempts_for(5) == MAX_ATTEMPTS


def test_two_failures_fall_back_to_the_adjacent_scene(paths, prepared):
    """specs/05 — 인접 씬 이미지 재사용. 카메라 워크 변경은 [7] 몫이라 기록으로 넘긴다."""
    run_id = prepared()
    client = FakeImageClient(fail_scenes={5: MAX_ATTEMPTS})

    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    record = scene_record(result, 5)
    images = paths.run_dir(run_id) / IMAGES_DIR
    assert record["status"] == FALLBACK
    assert record["reused_from"] == 4
    assert record["camera_variation_required"] is True
    assert (images / "5.png").read_bytes() == (images / "4.png").read_bytes()
    assert any("씬 5" in w and "[7]" in w for w in result.warnings)


def test_the_first_scene_falls_back_forward(paths, prepared):
    """앞 씬이 없으면 뒤에서 가져온다."""
    run_id = prepared()
    result = run_imagegen_stage(
        images=FakeImageClient(fail_scenes={1: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )
    assert scene_record(result, 1)["reused_from"] == 2


def test_a_failed_scene_does_not_stop_the_pipeline(paths, prepared):
    run_id = prepared()
    result = run_imagegen_stage(
        images=FakeImageClient(fail_scenes={5: MAX_ATTEMPTS, 9: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )

    assert result.passed
    assert result.count(FALLBACK) == 2
    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"][STAGE]["status"] == "done"
    assert state["stages"][STAGE]["fallback"] == 2


def test_no_image_at_all_is_a_stage_failure(paths, prepared):
    """폴백할 인접 씬이 하나도 없으면 여기서 멈춘다."""
    run_id = prepared()
    prompts = json.loads(
        (paths.run_dir(run_id) / PROMPTS_FILE).read_text(encoding="utf-8")
    )
    everything = {s["scene_id"]: MAX_ATTEMPTS for s in prompts["scenes"]}

    with pytest.raises(ImagegenStageError, match="한 장도"):
        run_imagegen_stage(
            images=FakeImageClient(fail_scenes=everything), run_id=run_id, paths=paths
        )

    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"][STAGE]["status"] == "failed"
    # 실패 이력은 남는다 — [11. report]가 읽는다
    assert len(read_record(paths, run_id)["scenes"]) == len(prompts["scenes"])


def test_unusable_provider_stops_without_burning_every_scene(paths, prepared):
    """프로바이더 자체가 못 쓰는 상태다 (여기서는 키 없음). 27씬을 각각 두 번씩
    실패시킬 이유가 없다. 키가 없으면 네트워크를 건드리기 전에 멈춘다 (ADR-0021)."""
    run_id = prepared()

    with pytest.raises(ImagegenStageError, match="GEMINI_API_KEY"):
        run_imagegen_stage(
            images=NanoBananaClient(), run_id=run_id, paths=paths,
            allow_missing_anchors=True,
        )

    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"][STAGE]["status"] == "failed"
    assert state["stages"][STAGE]["scenes_done"] == 0


# --- 방언 대조 (ADR-0027) ----------------------------------------------------


def test_wrong_dialect_is_blocked_before_the_first_paid_call(paths, prepared):
    """MJ 문법 문자열이 NB2로 들어가면 `--ar 9:16`이 그릴 대상이 된다.

    그 사실은 편당 과금이 끝난 뒤에야 드러나므로 호출 전에 막는다.
    """
    run_id = prepared(dialect="mj")

    with pytest.raises(DialectMismatch, match="방언"):
        run_imagegen_stage(
            images=NanoBananaClient(), run_id=run_id, paths=paths,
            allow_missing_anchors=True,
        )

    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    # 실패가 아니라 진입 금지다 — 고치는 곳은 [5]이고 무료다.
    assert state["stages"][STAGE]["status"] == "blocked"
    assert not (paths.run_dir(run_id) / "images").exists()


def test_fake_provider_takes_any_dialect(paths, prepared):
    """페이크는 방언을 가리지 않는다. 대조는 과금 어댑터의 규칙이다."""
    run_id = prepared(dialect="mj")

    result = run_imagegen_stage(
        images=FakeImageClient(), run_id=run_id, paths=paths,
    )

    assert result.passed


# --- 스타일 앵커 (ADR-0005) --------------------------------------------------


def test_paid_provider_is_blocked_when_there_are_no_anchors(paths, prepared):
    """지금 저장소 상태다 — assets/style_anchors/에 README뿐이다."""
    run_id = prepared()
    client = PaidFake()

    with pytest.raises(StyleAnchorsMissing, match="스타일 앵커"):
        run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    assert client.calls == []
    assert not (paths.run_dir(run_id) / IMAGES_DIR).exists()
    state = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"][STAGE]["status"] == "blocked"


def test_the_block_can_be_overridden_but_it_warns(paths, prepared):
    run_id = prepared()
    result = run_imagegen_stage(
        images=PaidFake(), run_id=run_id, paths=paths, allow_missing_anchors=True
    )

    assert result.passed
    assert any("앵커" in w for w in result.warnings)
    assert read_record(paths, run_id)["style_anchors"]["count"] == 0


def test_fake_provider_runs_without_anchors(paths, prepared):
    """페이크는 앵커 규칙의 대상이 아니다. 개발이 앵커 3장을 기다릴 이유가 없다."""
    run_id = prepared()
    result = run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    assert result.passed
    assert any("앵커" in w for w in result.warnings)


def test_every_call_carries_the_anchors(paths, prepared):
    """ADR-0005 — 모든 생성 호출에 레퍼런스로 첨부한다."""
    run_id = prepared()
    put_anchor(paths, "anchor-02.png")
    put_anchor(paths, "anchor-01.png")
    client = PaidFake()

    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    assert all(
        call["anchors"] == ("anchor-01.png", "anchor-02.png") for call in client.calls
    )
    assert result.warnings == []
    assert read_record(paths, run_id)["style_anchors"]["count"] == 2


# --- 돈: 같은 이미지를 두 번 사지 않는다 --------------------------------------


def test_rerun_spends_nothing(paths, prepared):
    run_id = prepared()
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)

    second = FakeImageClient()
    again = run_imagegen_stage(images=second, run_id=run_id, paths=paths)

    assert again.skipped
    assert second.calls == []


def test_force_rebuilds_everything(paths, prepared):
    run_id = prepared()
    run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)

    second = FakeImageClient()
    forced = run_imagegen_stage(
        images=second, run_id=run_id, paths=paths, force=True
    )

    assert not forced.skipped
    assert len(second.calls) == forced.scene_count


def test_a_fallback_scene_is_retried_and_the_rest_is_inherited(paths, prepared):
    """복사본은 결과가 아니라 땜질이다. 다시 시도하되 성공한 씬은 다시 사지 않는다."""
    run_id = prepared()
    first = run_imagegen_stage(
        images=FakeImageClient(fail_scenes={5: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )
    assert scene_record(first, 5)["status"] == FALLBACK

    second = FakeImageClient()
    again = run_imagegen_stage(images=second, run_id=run_id, paths=paths)

    assert not again.skipped
    assert second.scene_calls == [5]
    assert scene_record(again, 5)["status"] == GENERATED
    assert again.count(CACHED) == again.scene_count - 1
    assert again.calls == 1


def test_a_changed_prompt_invalidates_the_inherited_image(paths, prepared):
    """프롬프트가 바뀐 씬은 다시 만든다 — 안 바뀐 씬은 건드리지 않는다."""
    run_id = prepared()
    run_imagegen_stage(
        images=FakeImageClient(fail_scenes={5: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )

    prompts_path = paths.run_dir(run_id) / PROMPTS_FILE
    data = json.loads(prompts_path.read_text(encoding="utf-8"))
    data["scenes"][2]["prompt"] += "\nShot: 바뀐 지시"
    prompts_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    second = FakeImageClient()
    again = run_imagegen_stage(images=second, run_id=run_id, paths=paths)

    assert sorted(second.scene_calls) == [data["scenes"][2]["scene_id"], 5]
    assert again.calls == 2


# --- 실행 기록 (images.json) --------------------------------------------------


def test_record_covers_every_scene_for_the_report(paths, prepared):
    """[11. report]가 '실패 씬, 재시도 이력'을 여기서 읽는다 (specs/05)."""
    run_id = prepared()
    result = run_imagegen_stage(
        images=FakeImageClient(fail_scenes={5: 1, 9: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )
    record = read_record(paths, run_id)

    assert record["run_id"] == run_id
    assert record["provider"]["name"] == "fake"
    assert record["max_attempts"] == MAX_ATTEMPTS
    # 씬 5와 9가 각각 한 번씩 더 불렸다 (재시도 1회씩)
    assert record["calls"] == result.calls == result.scene_count + 2
    assert [s["scene_id"] for s in record["scenes"]] == [
        s["scene_id"] for s in result.scenes
    ]
    assert all("attempts" in s and "errors" in s for s in record["scenes"])
    assert scene_record(result, 9)["errors"] == record["scenes"][8]["errors"]


def test_record_survives_a_crash_partway_through(paths, prepared):
    """중간에 죽어도 앞서 산 이미지를 다시 사지 않게 씬마다 기록을 갱신한다."""
    run_id = prepared()
    boom = RuntimeError("호출 중 프로세스가 죽었다")

    def explode(scene_id, attempt):
        return boom if scene_id == 4 else RuntimeError("도달 불가")

    client = FakeImageClient(fail_scenes={4: 1}, error=explode)
    with pytest.raises(RuntimeError):
        run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    record = read_record(paths, run_id)
    assert [s["scene_id"] for s in record["scenes"]] == [1, 2, 3]

    resumed = FakeImageClient()
    result = run_imagegen_stage(images=resumed, run_id=run_id, paths=paths)
    assert resumed.scene_calls[0] == 4  # 1~3은 다시 사지 않는다
    assert result.count(CACHED) == 3


def test_a_crash_does_not_erase_what_was_already_paid_for(paths, prepared):
    """씬마다 기록을 덮어쓰되 아직 안 온 씬의 지문은 남긴다. 안 그러면 전부 다시 산다."""
    run_id = prepared()
    first = run_imagegen_stage(
        images=FakeImageClient(fail_scenes={20: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )
    before = {s["scene_id"]: s.get("digest") for s in read_record(paths, run_id)["scenes"]}

    # 3번 씬만 프롬프트가 바뀌었고, 그 씬을 만들다 프로세스가 죽는다
    prompts_path = paths.run_dir(run_id) / PROMPTS_FILE
    data = json.loads(prompts_path.read_text(encoding="utf-8"))
    data["scenes"][2]["prompt"] += "\nShot: 바뀐 지시"
    prompts_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    crasher = FakeImageClient(
        fail_scenes={3: 1}, error=lambda scene_id, attempt: RuntimeError("프로세스 사망")
    )
    with pytest.raises(RuntimeError):
        run_imagegen_stage(images=crasher, run_id=run_id, paths=paths)

    after = {s["scene_id"]: s.get("digest") for s in read_record(paths, run_id)["scenes"]}
    assert set(after) == set(before)
    assert {k: v for k, v in after.items() if k != 3} == {
        k: v for k, v in before.items() if k != 3
    }

    resumed = FakeImageClient()
    again = run_imagegen_stage(images=resumed, run_id=run_id, paths=paths)
    assert sorted(resumed.scene_calls) == [3, 20]
    assert again.count(CACHED) == first.scene_count - 2


def test_state_records_outputs_and_counts(paths, prepared):
    run_id = prepared()
    result = run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    entry = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )["stages"][STAGE]

    assert entry["status"] == "done"
    assert entry["generated"] == result.scene_count
    assert entry["calls"] == result.calls
    assert f"runs/{run_id}/{RECORD_FILE}" in entry["outputs"]


def test_summary_names_the_money(paths, prepared):
    run_id = prepared()
    result = run_imagegen_stage(images=FakeImageClient(), run_id=run_id, paths=paths)
    assert "[6]" in result.summary
    assert f"호출 {result.calls}회" in result.summary


# --- CLI ---------------------------------------------------------------------


def test_cli_defaults_to_the_paid_provider():
    """페이크가 기본이면 단색 PNG를 들고 다음 단계로 간다. 그쪽이 더 비싼 실수다.

    기본값은 **`[5]`의 기본 방언(`mj`)과 짝이 맞아야 한다** — 어긋나면 아무 옵션 없이
    돌린 파이프라인이 매번 `DialectMismatch`로 멈춘다 (ADR-0025·0027).
    """
    args = parse(["imagegen", "--slug", "abc"])
    assert args.provider == "midjourney"
    assert args.allow_missing_anchors is False
    assert args.run_id is None


def test_cli_takes_a_run_id_and_a_provider():
    args = parse(["imagegen", "--run-id", "20260810-x", "--provider", "fake"])
    assert args.run_id == "20260810-x" and args.provider == "fake"


def test_cli_rejects_an_unknown_provider():
    with pytest.raises(SystemExit):
        parse(["imagegen", "--slug", "abc", "--provider", "dall-e"])


# --- 동시 제출 (ADR-0031 §4) --------------------------------------------------
#
# 계약은 넷이다. 속도가 아니라 **속도를 내면서 깨지면 안 되는 것**이 계약이다.
#   1. 워커 수를 코드가 정하지 않는다 — 프로바이더에게 묻고, --jobs가 이긴다
#   2. 실제로 겹쳐 돈다 (안 겹치면 병렬이라 부를 수 없다)
#   3. images.json은 완료 순서가 아니라 씬 순서다 (ADR-0020 계약)
#   4. 429는 워커를 1로 줄인다


class CountingFake(FakeImageClient):
    """동시에 몇 개가 겹쳤는지 재는 페이크. `concurrency()`가 몇 번 불렸는지도 센다."""

    def __init__(self, *, limit: int = 3, hold: float = 0.02, **kw) -> None:
        super().__init__(**kw)
        self._limit = limit
        self._hold = hold
        self._lock = __import__("threading").Lock()
        self.active = 0
        self.peak = 0
        self.asked = 0

    def concurrency(self) -> int:
        self.asked += 1
        return self._limit

    def generate(self, request, *, timeout=None):
        import time

        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self._hold)
            return super().generate(request, timeout=timeout)
        finally:
            with self._lock:
                self.active -= 1


def test_worker_count_is_asked_of_the_provider_not_hardcoded(paths, prepared):
    """specs/05·ADR-0031 G3 — 워커 수는 코드에 적지 않고 프로바이더가 준다."""
    run_id = prepared()
    client = CountingFake(limit=3)
    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)
    assert client.asked == 1
    assert result.workers == 3


def test_jobs_flag_beats_the_provider(paths, prepared):
    """사람이 명시하면 프로바이더에게 묻지 않는다."""
    run_id = prepared()
    client = CountingFake(limit=3)
    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths, jobs=2)
    assert client.asked == 0
    assert result.workers == 2


def test_scenes_actually_overlap(paths, prepared):
    """겹쳐 돌지 않으면 병렬이 아니다 — 45분이 45분으로 남는다."""
    run_id = prepared()
    client = CountingFake(limit=3)
    run_imagegen_stage(images=client, run_id=run_id, paths=paths)
    assert client.peak > 1


def test_one_worker_stays_sequential(paths, prepared):
    """워커 1은 지금까지의 동작 그대로다. 프록시를 못 읽었을 때 떨어지는 자리다."""
    run_id = prepared()
    client = CountingFake(limit=1)
    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)
    assert client.peak == 1 and result.workers == 1


def test_record_keeps_scene_order_under_parallelism(paths, prepared):
    """ADR-0020 — images.json의 씬 순서는 prompts.json 순서다. 완료 순서가 아니다."""
    run_id = prepared()
    run_imagegen_stage(images=CountingFake(limit=3), run_id=run_id, paths=paths)
    record = read_record(paths, run_id)
    ids = [s["scene_id"] for s in record["scenes"]]
    assert ids == sorted(ids)
    prompts = json.loads(
        (paths.run_dir(run_id) / PROMPTS_FILE).read_text(encoding="utf-8")
    )
    assert ids == [s["scene_id"] for s in prompts["scenes"]]


def test_parallel_run_still_makes_every_image(paths, prepared):
    """병렬로 돌아도 씬 수 = 이미지 수다 (ADR-0019)."""
    run_id = prepared()
    result = run_imagegen_stage(
        images=CountingFake(limit=3), run_id=run_id, paths=paths
    )
    files = sorted((paths.run_dir(run_id) / IMAGES_DIR).glob("*.png"))
    assert len(files) == result.scene_count
    assert result.count(GENERATED) == result.scene_count


def test_cached_scenes_do_not_take_a_worker(paths, prepared):
    """이어받는 씬은 호출이 아니라 파일 확인이다. 워커를 잡으면 살 씬이 늦게 출발한다.

    씬 하나를 실패시켜 두면 다음 실행이 스킵되지 않는다 — 나머지는 이어받고 그 한 씬만
    산다. 그러면 워커도 1이어야 한다: 살 씬보다 많은 워커는 놀 뿐이고, 실행 기록의
    `workers`가 실제와 달라지면 나중에 실측을 그 숫자로 읽게 된다.
    """
    run_id = prepared()
    run_imagegen_stage(
        images=FakeImageClient(fail_scenes={5: MAX_ATTEMPTS}),
        run_id=run_id, paths=paths,
    )
    client = CountingFake(limit=3)
    result = run_imagegen_stage(images=client, run_id=run_id, paths=paths)
    assert result.count(CACHED) == result.scene_count - 1
    assert client.peak == 1
    assert result.workers == 1


class RateLimitedFake(CountingFake):
    """첫 호출만 429를 내는 페이크. 그 뒤로는 정상이다."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._first = True

    def generate(self, request, *, timeout=None):
        with self._lock:
            first, self._first = self._first, False
        if first:
            from shorts_factory.imagegen.base import ImageGenRateLimited

            raise ImageGenRateLimited("한도 도달")
        return super().generate(request, timeout=timeout)


def test_rate_limit_drops_the_workers_to_one(paths, prepared, monkeypatch):
    """ADR-0031 §4 — 429면 워커를 1로 줄인다. 계속 3개를 던지면 큐만 늘어난다."""
    import shorts_factory.stages.imagegen as stage

    monkeypatch.setattr(stage, "RATE_LIMIT_BACKOFF", 0)
    run_id = prepared()
    result = run_imagegen_stage(
        images=RateLimitedFake(limit=3), run_id=run_id, paths=paths
    )
    assert any("429" in w for w in result.warnings)
    assert result.passed  # 줄었을 뿐 단계는 끝까지 간다


def test_jobs_below_one_is_rejected(paths, prepared):
    run_id = prepared()
    with pytest.raises(ImagegenStageError):
        run_imagegen_stage(
            images=FakeImageClient(), run_id=run_id, paths=paths, jobs=0
        )


def test_cli_exposes_jobs():
    assert parse(["imagegen", "--slug", "abc"]).jobs is None
    assert parse(["imagegen", "--slug", "abc", "--jobs", "3"]).jobs == 3


# --- 대기 상한 (ADR-0035) ------------------------------------------------------
#
# 계약은 하나다. **단계는 숫자를 선언하지 않는다** — 워커 수와 같은 종류의 값이고,
# 잡을 얼마나 기다릴지 아는 것은 relax 큐를 보는 프로바이더뿐이다. 짧게 잡힌 상한은
# 이미 성공한 잡을 버린 뒤 재시도로 큐를 두 배로 만든다 (2026-08-19 실측).


def test_the_stage_does_not_declare_a_wait_budget():
    """ADR-0035 — `TIMEOUT` 상수가 다시 생기면 이 테스트가 걸린다."""
    import shorts_factory.stages.imagegen as stage

    assert not hasattr(stage, "TIMEOUT")


def test_the_provider_decides_the_wait_budget_by_default(paths, prepared):
    """기본값은 `None` = "프로바이더가 정한다". 단계가 숫자를 끼워 넣지 않는다."""
    run_id = prepared()
    client = FakeImageClient()
    run_imagegen_stage(images=client, run_id=run_id, paths=paths)

    assert client.calls
    assert {call["timeout"] for call in client.calls} == {None}


def test_an_explicit_timeout_reaches_the_provider(paths, prepared):
    """사람이 정하면 그 값이 그대로 어댑터까지 간다 (`--jobs`와 같은 구조)."""
    run_id = prepared()
    client = FakeImageClient()
    run_imagegen_stage(images=client, run_id=run_id, paths=paths, timeout=900)

    assert {call["timeout"] for call in client.calls} == {900}


def test_cli_exposes_timeout():
    assert parse(["imagegen", "--slug", "abc"]).timeout is None
    assert parse(["imagegen", "--slug", "abc", "--timeout", "900"]).timeout == 900
