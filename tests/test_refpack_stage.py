"""[4. refpack] — 씬별 실사 참조를 모은다. ADR-0030.

입력은 씬 계약(`scenes.json`) 하나다 (ADR-0017·0052). 세션은 페이크이고 내려받기는
주입한 `fetch`가 받으므로 네트워크도 구독 한도도 쓰지 않는다.

확인 대상:

- **산출은 둘이다** — `description`(서술)과 내려받은 파일. 강등 사다리는
  `첨부+서술 → 서술 → 없음`이고 **한쪽이 죽어도 다른 쪽이 돈다**
- **첨부 여부는 판단이 아니라 대조다** — 세션이 뭐라 적어 보내든 라이선스가 정한다
- **비어 있는 씬은 경고가 아니다** (specs/05 D-3). 도해 씬에는 참조가 없다
- ADR-0017 — 산출물은 `runs/{run_id}/` 아래뿐. `topics/`는 읽기만 한다
- specs/05 D-5 — 내려받기가 실패해도 단계 안에서 끝난다
"""

import json

import pytest

from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas.refs import (
    RECORD_FILE,
    attachable_licenses,
    attachments_of,
    description_of,
    max_images_per_scene,
    validate_refs,
)
from shorts_factory.stages.refpack import (
    REFS_DIR,
    TOOLS,
    RefpackStageError,
    run_refpack_stage,
)

from conftest import HOOVER, install_script

#: 아무 바이트나 좋다. 이 단계는 파일 내용을 해석하지 않는다.
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def contract_path(paths, slug=HOOVER):
    from conftest import load_script

    return paths.run_dir(load_script(slug)["run_id"]) / "scenes.json"


def script_of(paths, slug=HOOVER) -> dict:
    return json.loads(contract_path(paths, slug).read_text(encoding="utf-8"))


def scene_ids(paths, slug=HOOVER) -> list[int]:
    return [int(s["scene_id"]) for s in script_of(paths, slug)["scenes"]]


def response(ids, overrides=None) -> str:
    """씬마다 사진 한 장씩. `overrides`로 그 씬만 바꾼다."""
    overrides = overrides or {}
    out = []
    for sid in ids:
        item = {
            "scene_id": sid,
            "query": [f"query {sid}"],
            "description": f"씬 {sid}의 실물 서술",
            "images": [
                {
                    "source_url": f"https://example.org/{sid}.jpg",
                    "license": "public-domain",
                    "credit": "Some Archive",
                    "shows": "전경",
                }
            ],
        }
        item.update(overrides.get(sid, {}))
        out.append(item)
    return json.dumps({"scenes": out}, ensure_ascii=False)


def ok_fetch(url, timeout):
    return 200, JPEG


@pytest.fixture
def prepared(paths):
    """대본만 놓인 격리 루트. `(ids,)`를 돌려준다."""

    def _prepare(slug=HOOVER):
        install_script(paths, slug)
        return scene_ids(paths, slug)

    return _prepare


def run(paths, ids, *, overrides=None, fetch=ok_fetch, responses=None):
    llm = FakeLLMClient(responses=responses or [response(ids, overrides)])
    result = run_refpack_stage(
        HOOVER, llm=llm, paths=paths, fetch=fetch, download_interval=0
    )
    return result, llm


# --- 계약 -------------------------------------------------------------------


def test_the_output_keeps_the_contract(paths, prepared):
    ids = prepared()

    result, _ = run(paths, ids)

    assert result.valid
    assert validate_refs(result.refs) == []
    assert result.refs["run_id"] == script_of(paths)["run_id"]


def test_every_scene_of_the_script_gets_an_entry(paths, prepared):
    """소비 단계가 씬마다 조회한다. 항목이 없는 씬이 있으면 조회가 갈린다."""
    ids = prepared()

    result, _ = run(paths, ids)

    assert [s["scene_id"] for s in result.refs["scenes"]] == sorted(ids)


def test_the_output_lands_only_under_runs(paths, prepared):
    """ADR-0017 — `topics/`에는 아무것도 쓰지 않고, 씬 계약도 고치지 않는다."""
    ids = prepared()
    before = contract_path(paths).read_bytes()

    result, _ = run(paths, ids)

    assert result.path == paths.run_dir(result.run_id) / RECORD_FILE
    assert result.path.exists()
    assert contract_path(paths).read_bytes() == before
    assert not paths.topic_dir(HOOVER).exists()


# --- 두 경로와 강등 사다리 ---------------------------------------------------


def test_an_open_licence_is_downloaded_and_attachable(paths, prepared):
    ids = prepared()

    result, _ = run(paths, ids)

    target = ids[0]
    attached = attachments_of(result.refs, target)
    assert attached, "public-domain 사진은 첨부 경로를 타야 한다"
    assert (paths.run_dir(result.run_id) / attached[0]).read_bytes() == JPEG
    assert attached[0].startswith(f"{REFS_DIR}/{target}/")


@pytest.mark.parametrize("license_name", ["unknown", "copyrighted"])
def test_a_closed_licence_takes_the_description_path_only(paths, prepared, license_name):
    """불명·저작권 있음은 첨부하지 않는다. **서술은 그대로 산다** (ADR-0030)."""
    ids = prepared()
    target = ids[0]
    images = [
        {
            "source_url": "https://example.org/x.jpg",
            "license": license_name,
            "credit": "Someone",
        }
    ]

    result, _ = run(paths, ids, overrides={target: {"images": images}})

    assert validate_refs(result.refs) == []
    assert attachments_of(result.refs, target) == []
    assert description_of(result.refs, target)  # 서술 칸은 살아 있다
    assert not (paths.run_dir(result.run_id) / REFS_DIR / str(target)).exists()


def test_the_session_does_not_decide_attachability(paths, prepared):
    """세션이 `attachable`을 적어 보내도 라이선스가 이긴다.

    저작권이 걸린 축이라 판정 권한이 프롬프트로 새면 편마다 기준이 흔들린다.
    """
    ids = prepared()
    target = ids[0]
    images = [
        {
            "source_url": "https://example.org/x.jpg",
            "license": "copyrighted",
            "attachable": True,  # 세션의 주장
        }
    ]

    result, _ = run(paths, ids, overrides={target: {"images": images}})

    assert validate_refs(result.refs) == []
    assert attachments_of(result.refs, target) == []


def test_a_failed_download_demotes_to_description(paths, prepared):
    """specs/05 D-5 — 실패는 단계 안에서 끝난다. 사다리를 한 칸 내려갈 뿐이다."""
    ids = prepared()

    def dead_fetch(url, timeout):
        return 404, b""

    result, _ = run(paths, ids, fetch=dead_fetch)

    assert result.valid, "내려받기 실패가 단계를 실패시키면 안 된다"
    assert result.attached == 0
    assert result.described == len(ids)
    assert any("내려받지 못해" in w for w in result.warnings)


def test_no_download_keeps_the_record_but_takes_no_files(paths, prepared):
    """`--no-download` — 주소와 라이선스는 남기고 파일만 받지 않는다."""
    ids = prepared()

    result, _ = run(paths, ids, fetch=None)

    assert result.valid
    assert result.attached == 0
    assert not (paths.run_dir(result.run_id) / REFS_DIR).exists()
    first = result.refs["scenes"][0]["images"][0]
    assert first["source_url"] and first["license"] == "public-domain"


# --- 비어 있는 것은 정상이다 -------------------------------------------------


def test_an_empty_scene_is_not_a_warning(paths, prepared):
    """도해 씬과 실물이 없는 개념 씬에는 참조가 없다 (specs/05 D-3)."""
    ids = prepared()
    target = ids[0]
    empty = {"query": [], "description": "", "images": []}

    result, _ = run(paths, ids, overrides={target: empty})

    assert validate_refs(result.refs) == []
    assert result.warnings == []
    assert description_of(result.refs, target) == ""
    assert attachments_of(result.refs, target) == []


def test_a_scene_the_session_skipped_becomes_an_empty_entry(paths, prepared):
    ids = prepared()
    missing = ids[-1]
    llm = FakeLLMClient(
        responses=[
            json.dumps(
                {
                    "scenes": [
                        s
                        for s in json.loads(response(ids))["scenes"]
                        if s["scene_id"] != missing
                    ]
                },
                ensure_ascii=False,
            )
        ]
    )

    result = run_refpack_stage(
        HOOVER, llm=llm, paths=paths, fetch=ok_fetch, download_interval=0
    )

    assert validate_refs(result.refs) == []
    assert description_of(result.refs, missing) == ""
    assert result.warnings == []


# --- 세션 경계 ---------------------------------------------------------------


def test_a_string_scene_id_is_not_thrown_away(paths, prepared):
    """형식 슬립이지 계약 위반이 아니다. 버리면 그 씬의 조사가 통째로 사라진다."""
    ids = prepared()
    payload = json.loads(response(ids))
    for entry in payload["scenes"]:
        entry["scene_id"] = str(entry["scene_id"])

    result = run_refpack_stage(
        HOOVER,
        llm=FakeLLMClient(responses=[json.dumps(payload, ensure_ascii=False)]),
        paths=paths,
        fetch=ok_fetch,
        download_interval=0,
    )

    assert validate_refs(result.refs) == []
    assert result.described == len(ids)


def test_a_session_that_reports_nothing_is_a_warning(paths, prepared):
    """씬 하나가 빈 것은 정상이지만(D-3) 전부가 비면 세션이 일을 안 한 것이다."""
    ids = prepared()

    result = run_refpack_stage(
        HOOVER,
        llm=FakeLLMClient(responses=[json.dumps({"scenes": []})]),
        paths=paths,
        fetch=ok_fetch,
        download_interval=0,
    )

    assert validate_refs(result.refs) == []
    assert result.described == 0
    assert len(result.refs["scenes"]) == len(ids)
    assert any("하나도 보고하지 않았다" in w for w in result.warnings)


def test_the_session_gets_web_tools(paths, prepared):
    """`[4]`가 웹 도구를 받는 첫 단계다 (ADR-0030, ADR-0011과 다른 점)."""
    ids = prepared()

    _, llm = run(paths, ids)

    assert "WebSearch" in llm.calls[0]["allowed_tools"]
    assert set(llm.calls[0]["allowed_tools"]) == set(TOOLS)


def test_the_session_is_not_given_the_script_text(paths, prepared):
    """찾을 것은 그림의 대상이고 그것은 씬 계약의 그림 필드가 이미 말한다."""
    ids = prepared()

    _, llm = run(paths, ids)

    prompt = llm.calls[0]["prompt"]
    for scene in script_of(paths)["scenes"]:
        assert scene["text"] not in prompt
    assert str(script_of(paths)["scenes"][0]["subject"]) in prompt


def test_a_scene_outside_the_script_is_dropped_with_a_warning(paths, prepared):
    """세션이 지어낸 `scene_id`를 통과시키면 소비 단계가 없는 씬을 조회한다."""
    ids = prepared()
    payload = json.loads(response(ids))
    payload["scenes"].append(
        {"scene_id": 9999, "query": [], "description": "없는 씬", "images": []}
    )

    result = run_refpack_stage(
        HOOVER,
        llm=FakeLLMClient(responses=[json.dumps(payload, ensure_ascii=False)]),
        paths=paths,
        fetch=ok_fetch,
        download_interval=0,
    )

    assert [s["scene_id"] for s in result.refs["scenes"]] == sorted(ids)
    assert any("9999" in w for w in result.warnings)


def test_images_beyond_the_contract_limit_are_cut(paths, prepared):
    """상한은 `refs.schema.json`이 정한다 — 코드도 프롬프트도 선언하지 않는다."""
    ids = prepared()
    limit = max_images_per_scene()
    many = [
        {
            "source_url": f"https://example.org/{n}.jpg",
            "license": "public-domain",
        }
        for n in range(limit + 3)
    ]

    result, _ = run(paths, ids, overrides={ids[0]: {"images": many}})

    assert len(result.refs["scenes"][0]["images"]) == limit


def test_a_broken_script_stops_before_the_session(paths, prepared):
    """깨진 씬으로 검색어를 만들면 세션 시간만 쓰고 쓸 수 없는 참조가 나온다."""
    prepared()
    path = contract_path(paths)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["scenes"][0].pop("subject")
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    llm = FakeLLMClient(responses=[])
    with pytest.raises(RefpackStageError):
        run_refpack_stage(HOOVER, llm=llm, paths=paths, fetch=ok_fetch, download_interval=0)
    assert llm.calls == [], "계약 위반이면 세션을 부르지 않는다"


def test_a_finished_stage_is_not_run_twice(paths, prepared):
    ids = prepared()
    run(paths, ids)

    again = run_refpack_stage(
        HOOVER, llm=FakeLLMClient(responses=[]), paths=paths, fetch=ok_fetch,
        download_interval=0,
    )

    assert again.skipped
    assert validate_refs(again.refs) == []


# --- 계약 상수 ---------------------------------------------------------------


def test_the_attachable_licences_come_from_the_contract_file(paths):
    """ADR-0034 §3 — 코드가 목록을 선언하지 않는다."""
    from shorts_factory.schemas import vocab

    assert attachable_licenses() == tuple(
        vocab.load("refs.schema.json")["meta"]["attachable_licenses"]
    )


def test_the_user_agent_carries_a_contact():
    """위키미디어는 연락처가 없는 UA를 429로 끊는다 (실측 2026-08-27).

    QR 편 `[4]`가 사진 24장 중 17장을 이 이유로 잃었다 — 같은 순간 같은 파일이
    연락처를 단 UA에는 200으로 왔다. 설명만 담은 UA는 통과하지 못하므로 연락처를
    계약으로 못 박는다. **사람의 메일 주소는 쓰지 않는다** — 요청 헤더는 외부로
    나가는 자리라 저장소 주소를 쓴다.
    """
    from shorts_factory.stages.refpack import USER_AGENT

    assert "http" in USER_AGENT, f"UA에 연락처 URL이 없다: {USER_AGENT!r}"
    assert "@" not in USER_AGENT, f"UA에 메일 주소가 들어갔다: {USER_AGENT!r}"
