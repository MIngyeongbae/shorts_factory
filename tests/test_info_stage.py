"""[6i. info] 단계 계약 (ADR-0043, specs/05).

- **인포씬만** — `info` 필드가 있는 씬. 없으면 단계 전체가 스킵된다 (D-3)
- 라벨은 계약 문자열 그대로 편집 지시에 실린다 (ADR-0020)
- 검수(자소 대조·구도 보존)를 포함하고, 실패 시 재생성 1회 → 그래도 실패면 **강등**
- 강등은 파일의 부재로 전달된다 — `info/{scene_id}.jpg`를 지운다
"""

import json

import pytest

from conftest import HOOVER, install_script, load_script
from shorts_factory.config import write_text
from shorts_factory.imagegen.base import GeneratedImage, ImageGenError, ProviderNotConfigured
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.info import (
    STAGE,
    InfoStageError,
    edit_instruction,
    run_info_stage,
)
from timed_fixtures import install_images

SLUG = HOOVER
LABELS_A = ["블록 230개", "한 변 7.6m"]
LABELS_B = ["높이 221m"]


class FakeEditor:
    """NB2 편집 대역. 지시와 입력 경로를 기록한다."""

    def __init__(self, *, fail=None):
        self.calls = []
        #: `{scene_id 아님 — 호출 회차: 예외}`가 아니라 `{입력 파일 stem: 예외}`다.
        self.fail = fail or {}

    def edit(self, instruction, image_path, *, timeout=None):
        self.calls.append({"instruction": instruction, "image": image_path})
        stem = image_path.stem
        if stem in self.fail:
            raise self.fail[stem]
        # GeneratedImage가 시그니처를 검증하므로 진짜 JPEG 매직 바이트로 감싼다.
        payload = f"info-{stem}-v{len(self.calls)}".encode()
        return GeneratedImage(
            data=b"\xff\xd8\xff\xe0" + payload + b"\xff\xd9",
            mime_type="image/jpeg",
            request_id=f"edit-{stem}",
        )


def review_response(verdicts: dict[int, str]) -> str:
    return json.dumps(
        {"scenes": [
            {"scene_id": sid, "verdict": verdict, "reason": "사유"}
            for sid, verdict in verdicts.items()
        ]},
        ensure_ascii=False,
    )


@pytest.fixture
def prepared(paths):
    """인포씬 2개(1·3번)가 표시된 대본 + CLEAN 이미지가 있는 run."""
    install_script(paths, SLUG)
    script_path = paths.topic_dir(SLUG) / "06-script.json"
    script = load_script(SLUG)
    script["scenes"][0]["info"] = {"labels": LABELS_A}
    script["scenes"][2]["info"] = {"labels": LABELS_B}
    write_text(script_path, dump_json(script))

    run_id = script["run_id"]
    install_images(paths, run_id, [s["scene_id"] for s in script["scenes"]])
    return run_id


def record_of(paths, run_id):
    return json.loads(
        (paths.run_dir(run_id) / "info.json").read_text(encoding="utf-8")
    )


def state_of(paths, run_id):
    data = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    return data["stages"][STAGE]


# --- 지시 계약 -----------------------------------------------------------------


def test_instruction_carries_the_labels_verbatim():
    scene = {"scene_id": 1, "info": {"labels": LABELS_A}}
    text = edit_instruction(scene)
    for label in LABELS_A:
        assert label in text, "라벨은 계약 문자열 그대로다 (ADR-0020)"
    assert "보존" in text, "구도 보존이 편집 지시의 절반이다"
    assert "한 글자도" in text


# --- 인포씬이 없으면 스킵 (D-3) --------------------------------------------------


def test_no_info_scenes_skips_the_stage(paths):
    install_script(paths, SLUG)
    run_id = load_script(SLUG)["run_id"]
    editor = FakeEditor()

    result = run_info_stage(
        llm=FakeLLMClient([]), editor=editor, slug=SLUG, paths=paths,
    )

    assert result.scenes == []
    assert editor.calls == []
    assert state_of(paths, run_id)["status"] == "done"


# --- 통과 경로 -----------------------------------------------------------------


def test_happy_path_writes_info_images_and_the_record(paths, prepared):
    run_id = prepared
    editor = FakeEditor()
    llm = FakeLLMClient([review_response({1: "pass", 3: "pass"})])

    result = run_info_stage(llm=llm, editor=editor, slug=SLUG, paths=paths)

    assert (paths.run_dir(run_id) / "info" / "1.jpg").exists()
    assert (paths.run_dir(run_id) / "info" / "3.jpg").exists()
    assert len(editor.calls) == 2, "인포씬만 편집한다"
    assert [s["status"] for s in result.scenes] == ["generated", "generated"]

    record = record_of(paths, run_id)
    assert record["run_id"] == run_id
    assert record["scenes"][0]["labels"] == LABELS_A
    assert record["scenes"][0]["review"]["verdict"] == "pass"


def test_review_session_sees_both_frames_and_the_labels(paths, prepared):
    llm = FakeLLMClient([review_response({1: "pass", 3: "pass"})])
    run_info_stage(llm=llm, editor=FakeEditor(), slug=SLUG, paths=paths)

    prompt = llm.calls[0]["prompt"]
    assert "images/1.png" in prompt, "CLEAN 원본을 함께 보여 준다 (구도 대조)"
    assert "info/1.jpg" in prompt
    for label in LABELS_A + LABELS_B:
        assert label in prompt, "검수는 계약 문자열과 대조한다"
    assert "Read" in llm.calls[0]["allowed_tools"]


def test_second_run_skips(paths, prepared):
    llm = FakeLLMClient([review_response({1: "pass", 3: "pass"})])
    run_info_stage(llm=llm, editor=FakeEditor(), slug=SLUG, paths=paths)

    editor = FakeEditor()
    again = run_info_stage(
        llm=FakeLLMClient([]), editor=editor, slug=SLUG, paths=paths,
    )
    assert again.skipped
    assert editor.calls == []


# --- 검수 실패 → 재생성 1회 → 강등 ----------------------------------------------


def test_redo_regenerates_once_and_reviews_again(paths, prepared):
    run_id = prepared
    llm = FakeLLMClient([
        review_response({1: "redo", 3: "pass"}),
        review_response({1: "pass"}),
    ])
    editor = FakeEditor()

    result = run_info_stage(llm=llm, editor=editor, slug=SLUG, paths=paths)

    assert len(editor.calls) == 3, "redo 씬 하나만 다시 편집한다"
    by_id = {s["scene_id"]: s for s in result.scenes}
    assert by_id[1]["status"] == "regenerated"
    assert by_id[1]["attempts"] == 2
    assert by_id[3]["status"] == "generated"
    assert (paths.run_dir(run_id) / "info" / "1.jpg").exists()


def test_redo_twice_demotes_and_removes_the_file(paths, prepared):
    """상한 1회 — 재생성본도 기각이면 강등이고, 파일을 남기지 않는다.

    파일이 남으면 그것이 `[7]`의 끝 프레임이 된다 — 검수에 떨어진 그림으로 영상을
    만들게 된다.
    """
    run_id = prepared
    llm = FakeLLMClient([
        review_response({1: "redo", 3: "pass"}),
        review_response({1: "redo"}),
    ])

    result = run_info_stage(llm=llm, editor=FakeEditor(), slug=SLUG, paths=paths)

    by_id = {s["scene_id"]: s for s in result.scenes}
    assert by_id[1]["status"] == "demoted"
    assert by_id[1]["file"] is None
    assert not (paths.run_dir(run_id) / "info" / "1.jpg").exists()
    assert (paths.run_dir(run_id) / "info" / "3.jpg").exists(), "통과 씬은 산다"
    assert any("강등" in w for w in result.warnings), "조용한 강등은 없다"
    assert state_of(paths, run_id)["demoted"] == 1


def test_edit_failure_demotes_the_scene(paths, prepared):
    editor = FakeEditor(fail={"1": ImageGenError("생성 실패")})
    llm = FakeLLMClient([review_response({3: "pass"})])

    result = run_info_stage(llm=llm, editor=editor, slug=SLUG, paths=paths)

    by_id = {s["scene_id"]: s for s in result.scenes}
    assert by_id[1]["status"] == "demoted"
    assert by_id[3]["status"] == "generated"


def test_provider_down_demotes_the_rest_without_calls(paths, prepared):
    editor = FakeEditor(fail={"1": ProviderNotConfigured("키 없음")})

    result = run_info_stage(
        llm=FakeLLMClient([]), editor=editor, slug=SLUG, paths=paths,
    )

    assert all(s["status"] == "demoted" for s in result.scenes)
    assert len(editor.calls) == 1, "프로바이더 전체가 막히면 남은 씬은 시도하지 않는다"


def test_broken_review_demotes_instead_of_passing(paths, prepared):
    """검수가 깨지면 통과로 넘기지 않는다 — 검수 없는 생성 텍스트는 금지다 (ADR-0043)."""
    run_id = prepared
    llm = FakeLLMClient(["JSON이 아닌 응답"])

    result = run_info_stage(llm=llm, editor=FakeEditor(), slug=SLUG, paths=paths)

    assert all(s["status"] == "demoted" for s in result.scenes)
    assert not (paths.run_dir(run_id) / "info" / "1.jpg").exists()


# --- 입력 계약 -----------------------------------------------------------------


def test_missing_clean_image_stops_before_any_call(paths):
    install_script(paths, SLUG)
    script_path = paths.topic_dir(SLUG) / "06-script.json"
    script = load_script(SLUG)
    script["scenes"][0]["info"] = {"labels": LABELS_A}
    write_text(script_path, dump_json(script))
    # CLEAN 이미지를 설치하지 않는다

    editor = FakeEditor()
    with pytest.raises(InfoStageError, match=r"\[6\. imagegen\]"):
        run_info_stage(llm=FakeLLMClient([]), editor=editor, slug=SLUG, paths=paths)
    assert editor.calls == []
