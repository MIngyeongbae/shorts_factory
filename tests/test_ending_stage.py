"""[8. ending] 단계 계약 (specs/05-pipeline.md, specs/03 「엔딩 실사」, ADR-0055).

이 단계의 급소는 셋이고 전부 저작권·초상권이 걸린 축이라 계약으로 못박는다.

- **게시 가능 라이선스만 화면에 나간다.** 대조는 기계가 하고 세션이 뒤집을 수 없다
- **표시 의무를 못 지킬 사진은 후보에도 안 오른다** (`cc-by` 계열에 `credit` 부재)
- **인물 판정은 세션이 한다** — 라이선스가 답할 수 없는 축이라 사람 눈이 필요하다

그리고 무수정 표시(크롭·줌 없음)와 하드컷 진입은 **기하와 라이선스가 함께 정한 값**이라
자유화 대상이 아니다.
"""

import json

import pytest
from conftest import PISA, install_script
from timed_fixtures import install_run

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas import ending as ending_schema
from shorts_factory.stages.ending import (
    STAGE,
    EndingStageError,
    clip_lengths,
    collect_candidates,
    parse_verdicts,
    render_candidates,
    run_ending_stage,
    select,
)
from shorts_factory.video.ending import build_filter, credit_line, credits_document
from shorts_factory.video.fake import FakeFFmpeg

RECORD = ending_schema.RECORD_FILE


def image(file, license_name="public-domain", *, credit="", url=None, shows=""):
    entry = {
        "source_url": url or f"https://example.org/{file}",
        "license": license_name,
        "attachable": ending_schema.is_publishable(license_name),
    }
    if file:
        entry["file"] = file
    if credit:
        entry["credit"] = credit
    if shows:
        entry["shows"] = shows
    return entry


def refs_document(run_id="20260822-x", scenes=None):
    return {
        "run_id": run_id,
        "scenes": scenes
        or [
            {
                "scene_id": 3,
                "query": [],
                "description": "돌탑",
                "images": [image("refs/3/01.jpg", shows="전경")],
            }
        ],
    }


def install_refs(paths, run_id, scenes, *, files=True):
    """`[4]`가 끝난 상태 — `refs.json`과 내려받은 파일."""
    run_dir = paths.run_dir(run_id)
    document = refs_document(run_id, scenes)
    write_text(run_dir / "refs.json", dump_json(document))
    if files:
        for entry in scenes:
            for img in entry["images"]:
                if not img.get("file"):
                    continue
                target = run_dir / img["file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fake-jpeg")
    return document


def verdicts_json(*rows):
    return json.dumps({"photos": list(rows)}, ensure_ascii=False)


def keep(source, order):
    return {"id": source, "verdict": "keep", "order": order, "reason": "실물이 보인다"}


def reject(source, reason="인물이 식별된다"):
    return {"id": source, "verdict": "reject", "order": None, "reason": reason}


@pytest.fixture
def run_id(paths):
    """`[3]`·`[7]`이 끝난 run. 엔딩은 `[9]`와 이어 붙으므로 같은 픽스처를 쓴다."""
    run_id, _document = install_run(paths, PISA)
    return run_id


def run(paths, run_id, responses, *, ffmpeg=None, **kwargs):
    return run_ending_stage(
        llm=FakeLLMClient(responses),
        run_id=run_id,
        paths=paths,
        runner=ffmpeg or FakeFFmpeg(),
        **kwargs,
    )


def state_of(paths, run_id):
    data = json.loads((paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][STAGE]


def record_of(paths, run_id):
    return json.loads((paths.run_dir(run_id) / RECORD).read_text(encoding="utf-8"))


# --- ① 기계 대조: 게시 가능 라이선스와 표시 의무 ------------------------------


@pytest.mark.parametrize("license_name", ending_schema.publishable_licenses())
def test_publishable_licenses_reach_the_review(license_name):
    credit = "촬영자" if ending_schema.needs_credit(license_name) else ""
    kept, rejected = collect_candidates(
        refs_document(scenes=[{
            "scene_id": 1, "query": [], "description": "",
            "images": [image("refs/1/01.jpg", license_name, credit=credit)],
        }])
    )

    assert [c["source"] for c in kept] == ["refs/1/01.jpg"]
    assert rejected == []


@pytest.mark.parametrize("license_name", ["copyrighted", "unknown"])
def test_unpublishable_licenses_never_reach_the_screen(license_name):
    """`[4]`가 내려받지 않는 값이지만 대조는 여기서도 한다 — 화면에 나가는 축이다."""
    kept, rejected = collect_candidates(
        refs_document(scenes=[{
            "scene_id": 1, "query": [], "description": "",
            "images": [image("refs/1/01.jpg", license_name)],
        }])
    )

    assert kept == []
    assert rejected[0]["by"] == ending_schema.BY_MACHINE
    assert license_name in rejected[0]["reason"]


@pytest.mark.parametrize("license_name", ending_schema.credit_required_licenses())
def test_attribution_required_photo_without_credit_is_dropped(license_name):
    """표시 의무를 지킬 수 없으면 **후보에도 못 오른다.** 판단이 아니라 대조다."""
    kept, rejected = collect_candidates(
        refs_document(scenes=[{
            "scene_id": 1, "query": [], "description": "",
            "images": [image("refs/1/01.jpg", license_name, credit="")],
        }])
    )

    assert kept == []
    assert rejected[0]["by"] == ending_schema.BY_MACHINE
    assert "credit" in rejected[0]["reason"]


def test_photos_without_a_downloaded_file_are_skipped():
    """서술 경로만 탄 사진은 화면에 올릴 파일이 없다 — 기각이 아니라 무관이다."""
    kept, rejected = collect_candidates(
        refs_document(scenes=[{
            "scene_id": 1, "query": [], "description": "",
            "images": [image("", "public-domain")],
        }])
    )

    assert kept == []
    assert rejected == []


def test_the_same_photo_is_considered_once():
    """같은 사진이 두 씬의 참조로 잡혀도 엔딩에는 한 번만 나간다."""
    shared = "https://example.org/same.jpg"
    kept, _rejected = collect_candidates(
        refs_document(scenes=[
            {"scene_id": 1, "query": [], "description": "",
             "images": [image("refs/1/01.jpg", url=shared)]},
            {"scene_id": 2, "query": [], "description": "",
             "images": [image("refs/2/01.jpg", url=shared)]},
        ])
    )

    assert [c["source"] for c in kept] == ["refs/1/01.jpg"]


def test_review_never_sees_the_licence():
    """판정은 그림만 보고 한다 — 출처가 좋다는 이유로 통과시키면 안 된다 (ADR-0038 태도)."""
    rendered = render_candidates([
        {"source": "refs/1/01.jpg", "license": "cc-by-sa",
         "credit": "국가유산청", "source_url": "https://example.org/a.jpg", "shows": "전경"},
    ])

    assert "refs/1/01.jpg" in rendered
    assert "cc-by-sa" not in rendered
    assert "국가유산청" not in rendered
    assert "example.org" not in rendered


# --- ② 판정 읽기 ---------------------------------------------------------------


def test_missing_verdict_falls_to_reject():
    """엔딩은 없어도 되는 마감이라 **모자란 쪽으로 떨어뜨린다** (D-5)."""
    verdicts, warnings = parse_verdicts({"photos": []}, {"refs/1/01.jpg"})

    assert verdicts["refs/1/01.jpg"]["verdict"] == "reject"
    assert any("판정하지 않았다" in w for w in warnings)


def test_unknown_verdict_falls_to_reject():
    verdicts, warnings = parse_verdicts(
        {"photos": [{"id": "refs/1/01.jpg", "verdict": "maybe"}]}, {"refs/1/01.jpg"}
    )

    assert verdicts["refs/1/01.jpg"]["verdict"] == "reject"
    assert warnings


def test_invented_candidate_is_dropped_with_a_warning():
    verdicts, warnings = parse_verdicts(
        {"photos": [keep("refs/9/09.jpg", 1)]}, {"refs/1/01.jpg"}
    )

    assert "refs/9/09.jpg" not in verdicts
    assert any("후보에 없는" in w for w in warnings)


# --- 고르기 --------------------------------------------------------------------


def candidates(*sources):
    return [
        {"source": s, "source_url": f"https://example.org/{s}",
         "license": "public-domain", "credit": "", "shows": ""}
        for s in sources
    ]


def test_session_order_decides_the_display_order():
    rows = candidates("a.jpg", "b.jpg")
    verdicts = {"a.jpg": {"verdict": "keep", "order": 2, "reason": ""},
                "b.jpg": {"verdict": "keep", "order": 1, "reason": ""}}

    chosen, _rejected = select(rows, verdicts, limit=3)

    assert [c["source"] for c in chosen] == ["b.jpg", "a.jpg"]


def test_overflow_is_recorded_as_truncation_not_rejection():
    rows = candidates("a.jpg", "b.jpg", "c.jpg")
    verdicts = {
        s: {"verdict": "keep", "order": i, "reason": ""}
        for i, s in enumerate(["a.jpg", "b.jpg", "c.jpg"], start=1)
    }

    chosen, rejected = select(rows, verdicts, limit=2)

    assert [c["source"] for c in chosen] == ["a.jpg", "b.jpg"]
    assert rejected[0]["source"] == "c.jpg"
    assert "잘렸다" in rejected[0]["reason"]


def test_keep_without_order_still_survives():
    """순서 하나가 비었다고 쓸 수 있는 사진을 버리지 않는다."""
    rows = candidates("a.jpg", "b.jpg")
    verdicts = {"a.jpg": {"verdict": "keep", "order": None, "reason": ""},
                "b.jpg": {"verdict": "keep", "order": 1, "reason": ""}}

    chosen, _rejected = select(rows, verdicts, limit=3)

    assert [c["source"] for c in chosen] == ["b.jpg", "a.jpg"]


# --- ③ 렌더 기하 ---------------------------------------------------------------


def test_only_the_last_cut_has_no_tail():
    """specs/05 `[7]`의 클립 기하 그대로 — 꼬리는 다음 컷과 겹치는 몫이다 (ADR-0024)."""
    assert clip_lengths(3, seconds=2.4, dissolve=0.6) == [3.0, 3.0, 2.4]
    assert clip_lengths(1, seconds=2.4, dissolve=0.6) == [2.4]


def test_the_photo_is_never_cropped_or_zoomed():
    """**이 결정의 급소다** — SA가 영상으로 번지지 않는 근거가 무수정 위에 서 있다."""
    chain = build_filter()

    assert "crop" not in chain
    assert "zoompan" not in chain
    assert "force_original_aspect_ratio=decrease" in chain
    assert "pad=" in chain


def test_credit_is_burned_only_when_both_pieces_exist():
    assert "drawtext" not in build_filter(credit_textfile="c.txt", fontfile=None)
    assert "drawtext" not in build_filter(credit_textfile=None, fontfile="f.ttf")
    assert "drawtext" in build_filter(credit_textfile="c.txt", fontfile="f.ttf")


def test_credit_line_names_the_author_and_the_licence():
    assert credit_line("국가유산청", "cc-by-sa") == "국가유산청 · CC BY-SA"
    assert credit_line("", "public-domain") == "Public Domain"


def test_credits_document_keeps_the_source_url():
    """화면 한 줄이 담지 못하는 것까지 남는다 — 굽기가 강등돼도 이 파일은 산다."""
    text = credits_document([
        {"index": 1, "credit": "촬영자", "license": "cc-by",
         "source_url": "https://example.org/a.jpg", "shows": "전경"},
    ])

    assert "촬영자" in text and "CC BY" in text
    assert "https://example.org/a.jpg" in text


# --- 단계 통과 경로 -------------------------------------------------------------


def test_stage_renders_the_chosen_photos_in_order(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [
            image("refs/3/01.jpg", shows="전경"),
            image("refs/3/02.jpg", shows="세부"),
        ]},
    ])

    result = run(paths, run_id, [
        verdicts_json(keep("refs/3/02.jpg", 2), keep("refs/3/01.jpg", 1))
    ])

    assert [p["source"] for p in result.photos] == ["refs/3/01.jpg", "refs/3/02.jpg"]
    assert [p["index"] for p in result.photos] == [1, 2]
    assert [p["file"] for p in result.photos] == ["ending/1.mp4", "ending/2.mp4"]
    assert result.errors == []
    assert ending_schema.validate_ending(record_of(paths, run_id)) == []


def test_stage_writes_the_credits_file(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [
            image("refs/3/01.jpg", "cc-by", credit="촬영자"),
        ]},
    ])

    run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))])

    credits = (paths.run_dir(run_id) / "ending" / "credits.txt").read_text(encoding="utf-8")
    assert "촬영자" in credits


def test_credit_burn_degrades_when_the_font_asset_is_missing(paths, run_id):
    """강등 사다리 `굽기+기록 → 기록` — 리포에 폰트가 없는 지금 상태가 이 경로다."""
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [
            image("refs/3/01.jpg", "cc-by", credit="촬영자"),
        ]},
    ])

    result = run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))])

    assert result.photos[0]["burned"] is False
    assert result.photos[0]["credit_line"] == "촬영자 · CC BY"
    assert any("크레딧" in w for w in result.warnings)
    assert "촬영자" in (paths.run_dir(run_id) / "ending" / "credits.txt").read_text(
        encoding="utf-8"
    )


def test_render_command_never_touches_the_source_photo(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])
    ffmpeg = FakeFFmpeg()

    run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))], ffmpeg=ffmpeg)

    assert ffmpeg.option_of(ffmpeg.last, "-i") == "refs/3/01.jpg"
    assert "crop" not in ffmpeg.vf and "zoompan" not in ffmpeg.vf
    assert ffmpeg.calls[0]["kwargs"]["cwd"] == str(paths.run_dir(run_id))


# --- 없는 것이 정상인 편 --------------------------------------------------------


def test_no_refs_means_no_ending_and_no_llm_call(paths, run_id):
    """`[4]`를 안 돌린 편은 엔딩이 없다. 부재는 경고가 아니다 (D-3)."""
    llm = FakeLLMClient([])
    result = run_ending_stage(llm=llm, run_id=run_id, paths=paths, runner=FakeFFmpeg())

    assert result.photos == []
    assert llm.calls == []
    assert not (paths.run_dir(run_id) / RECORD).exists()
    assert state_of(paths, run_id)["status"] == "done"


def test_all_rejected_means_no_contract_file(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])

    result = run(paths, run_id, [verdicts_json(reject("refs/3/01.jpg"))])

    assert result.photos == []
    assert not (paths.run_dir(run_id) / RECORD).exists()
    assert result.rejected_by(ending_schema.BY_REVIEW) == 1
    assert state_of(paths, run_id)["photos"] == 0


def test_a_stale_contract_is_removed_when_nothing_survives(paths, run_id):
    """지난 실행의 `ending.json`이 남으면 `[9]`가 없는 클립을 찾는다."""
    stale = paths.run_dir(run_id) / RECORD
    write_text(stale, dump_json({"run_id": run_id, "photos": []}))
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])

    run(paths, run_id, [verdicts_json(reject("refs/3/01.jpg"))])

    assert not stale.exists()


def test_only_unpublishable_photos_means_no_llm_call(paths, run_id):
    """대조에서 후보가 0장이면 세션을 부르지 않는다 — 볼 것이 없다."""
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "",
         "images": [image("refs/3/01.jpg", "copyrighted")]},
    ])
    llm = FakeLLMClient([])

    result = run_ending_stage(llm=llm, run_id=run_id, paths=paths, runner=FakeFFmpeg())

    assert llm.calls == []
    assert result.photos == []


# --- 실패 정책 -----------------------------------------------------------------


def test_a_broken_session_output_stops_the_stage(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])

    with pytest.raises(EndingStageError):
        run(paths, run_id, ["JSON이 아니다"])

    assert not (paths.run_dir(run_id) / RECORD).exists()
    assert state_of(paths, run_id)["status"] == "failed"


def test_one_failed_render_does_not_lose_the_others(paths, run_id):
    """컷 하나의 실패로 엔딩 전체를 버리지 않는다 (D-5)."""
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [
            image("refs/3/01.jpg"), image("refs/3/02.jpg"),
        ]},
    ])

    class FailsFirst(FakeFFmpeg):
        def __call__(self, cmd, **kwargs):
            if len(self.calls) == 0:
                self.calls.append({"cmd": list(cmd), "kwargs": kwargs})
                import subprocess
                return subprocess.CompletedProcess(list(cmd), 1, "", "boom")
            return super().__call__(cmd, **kwargs)

    result = run(
        paths, run_id,
        [verdicts_json(keep("refs/3/01.jpg", 1), keep("refs/3/02.jpg", 2))],
        ffmpeg=FailsFirst(),
    )

    assert [p["source"] for p in result.photos] == ["refs/3/02.jpg"]
    assert [p["index"] for p in result.photos] == [1]
    assert ending_schema.validate_ending(record_of(paths, run_id)) == []


def test_a_vanished_download_is_skipped(paths, run_id):
    install_refs(
        paths, run_id,
        [{"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]}],
        files=False,
    )

    result = run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))])

    assert result.photos == []
    assert not (paths.run_dir(run_id) / RECORD).exists()


# --- 재실행 --------------------------------------------------------------------


def test_a_finished_stage_is_skipped(paths, run_id):
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])
    run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))])

    llm = FakeLLMClient([])
    again = run_ending_stage(llm=llm, run_id=run_id, paths=paths, runner=FakeFFmpeg())

    assert again.skipped
    assert llm.calls == []
    assert len(again.photos) == 1


def test_an_empty_ending_is_also_remembered(paths, run_id):
    """엔딩 없음도 완료다 — 다시 돌려도 세션을 또 부르지 않는다."""
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])
    run(paths, run_id, [verdicts_json(reject("refs/3/01.jpg"))])

    llm = FakeLLMClient([])
    again = run_ending_stage(llm=llm, run_id=run_id, paths=paths, runner=FakeFFmpeg())

    assert again.skipped and llm.calls == []


def test_slug_resolves_the_run(paths):
    install_script(paths, PISA)
    llm = FakeLLMClient([])

    result = run_ending_stage(llm=llm, slug=PISA, paths=paths, runner=FakeFFmpeg())

    assert result.run_id.endswith(PISA)
    assert result.photos == []


def test_the_stage_writes_nothing_under_topics(paths, run_id):
    """ADR-0017 — 2부 산출물은 전부 run 디렉터리 아래다."""
    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])
    before = sorted(p.name for p in paths.topics.rglob("*"))

    run(paths, run_id, [verdicts_json(keep("refs/3/01.jpg", 1))])

    assert sorted(p.name for p in paths.topics.rglob("*")) == before


# --- [8] → [9] 관통 -------------------------------------------------------------


def test_the_ending_reaches_the_timeline(paths, run_id):
    """두 단계를 파일 규약 하나로만 묶는다 — `[9]`는 `ending.json`만 보고 붙인다."""
    from shorts_factory.stages.assemble import run_assemble_stage

    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [
            image("refs/3/01.jpg"), image("refs/3/02.jpg"),
        ]},
    ])
    ending = run(
        paths, run_id,
        [verdicts_json(keep("refs/3/01.jpg", 1), keep("refs/3/02.jpg", 2))],
    )

    ffmpeg = FakeFFmpeg()
    assembled = run_assemble_stage(run_id, paths=paths, runner=ffmpeg)

    assert assembled.ending_cuts == len(ending.photos) == 2
    inputs = [ffmpeg.last[i + 1] for i, a in enumerate(ffmpeg.last) if a == "-i"]
    assert inputs[-2:] == [p["file"] for p in ending.photos]
    assert assembled.passed


def test_an_empty_ending_leaves_assemble_untouched(paths, run_id):
    """전부 기각된 편도 `[9]`가 그대로 돈다 — 부재가 곧 신호다 (D-3)."""
    from shorts_factory.stages.assemble import run_assemble_stage

    install_refs(paths, run_id, [
        {"scene_id": 3, "query": [], "description": "", "images": [image("refs/3/01.jpg")]},
    ])
    run(paths, run_id, [verdicts_json(reject("refs/3/01.jpg"))])

    assembled = run_assemble_stage(run_id, paths=paths, runner=FakeFFmpeg())

    assert assembled.passed and assembled.ending_cuts == 0
