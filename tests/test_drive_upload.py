"""[10. upload] 계약 테스트 — 이름 규칙 · resumable 전송 · 재실행 (ADR-0084).

네트워크는 없다. 경계(`storage/gdrive.py`)는 페이크 `HeaderTransport`로, 단계는 페이크
클라이언트로 잰다 — `conftest`가 유료 키를 지우는 것과 같은 태도다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from shorts_factory.stages import upload as upload_stage
from shorts_factory.storage import gdrive

KO_TITLE = "코치닐 — 은 다음으로 비쌌던 빨강, 정체는 벌레였다"
JA_TITLE = "コチニール — 銀の次に高かった赤、その正体は虫だった"
EN_TITLE = "Cochineal — the red that cost almost as much as silver was an insect"


# --- 이름 규칙: 순수 함수라 정답을 그대로 잰다 ---------------------------------


def test_safe_name_keeps_the_title_as_the_human_wrote_it():
    """줄이지도, 로마자로 바꾸지도, 문장부호를 털지도 않는다 — 사람이 읽을 이름이다."""
    assert gdrive.safe_name(KO_TITLE) == KO_TITLE
    assert gdrive.safe_name(JA_TITLE) == JA_TITLE
    assert gdrive.safe_name(EN_TITLE) == EN_TITLE


def test_safe_name_drops_control_characters_and_collapses_space():
    assert gdrive.safe_name("앞\t뒤\n제목  ") == "앞 뒤 제목"


def test_safe_name_refuses_an_empty_title():
    """이름을 지어내지 않는다 — 제목이 없는 편은 없다."""
    with pytest.raises(gdrive.DriveError):
        gdrive.safe_name("   \n ")


def test_folder_name_is_the_upload_day_then_the_korean_title():
    assert gdrive.folder_name(date(2026, 8, 31), KO_TITLE) == f"2026-08-31 {KO_TITLE}"


def test_escape_query_protects_an_apostrophe_in_the_title():
    """`Europe's …` 같은 제목이 드라이브 `q` 문자열을 깨지 않게."""
    assert gdrive.escape_query("Europe's red") == "Europe\\'s red"


def test_content_range_end_is_inclusive():
    assert gdrive.content_range(0, 10, 25) == "bytes 0-9/25"
    assert gdrive.content_range(10, 15, 25) == "bytes 10-24/25"


def test_next_offset_reads_the_range_header_and_falls_back():
    assert gdrive.next_offset("bytes=0-9", fallback=0) == 10
    assert gdrive.next_offset("", fallback=7) == 7
    assert gdrive.next_offset("weird", fallback=7) == 7


# --- 경계: 페이크 transport로 전송 규약을 잰다 ---------------------------------


@pytest.fixture
def drive_env(monkeypatch):
    for key in gdrive.ENV_KEYS:
        monkeypatch.setenv(key, "x")
    monkeypatch.delenv(gdrive.PARENT_ENV, raising=False)


class FakeTransport:
    """`(method, url, headers, body, timeout) -> (status, headers, body)`."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body}
        )
        if not self.script:
            raise AssertionError(f"예상 밖의 호출: {method} {url}")
        return self.script.pop(0)


def _token(status: int = 200):
    return status, {}, json.dumps({"access_token": "tok"}).encode()


def test_the_access_token_is_fetched_once_and_reused(drive_env):
    transport = FakeTransport([
        _token(),
        (200, {}, json.dumps({"files": []}).encode()),
        (200, {}, json.dumps({"id": "F1"}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport)
    client.ensure_folder("폴더", None)
    token_calls = [c for c in transport.calls if c["url"] == gdrive.TOKEN_URL]
    assert len(token_calls) == 1
    assert b"grant_type=refresh_token" in token_calls[0]["body"]


def test_an_existing_folder_is_reused_not_created(drive_env):
    transport = FakeTransport([
        _token(),
        (200, {}, json.dumps({"files": [{"id": "OLD", "name": "폴더"}]}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport)
    assert client.ensure_folder("폴더", None) == "OLD"
    assert all(c["method"] != "POST" or c["url"] == gdrive.TOKEN_URL for c in transport.calls)


def test_a_new_folder_carries_the_parent(drive_env):
    transport = FakeTransport([
        _token(),
        (200, {}, json.dumps({"files": []}).encode()),
        (200, {}, json.dumps({"id": "NEW"}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport)
    assert client.ensure_folder("폴더", "PARENT") == "NEW"
    created = transport.calls[-1]
    assert created["method"] == "POST"
    assert json.loads(created["body"])["parents"] == ["PARENT"]


def test_a_big_file_goes_up_in_chunks_and_reports_content_range(drive_env, tmp_path):
    """편당 90~120MB라 단일 요청을 못 쓴다 (ADR-0084 맥락 3) — 청크가 실제로 갈리는지 본다."""
    video = tmp_path / "timeline.ko.mp4"
    video.write_bytes(b"0123456789")
    transport = FakeTransport([
        _token(),
        (200, {"Location": "https://session/1"}, b""),
        (308, {"Range": "bytes=0-3"}, b""),
        (308, {"Range": "bytes=0-7"}, b""),
        (200, {}, json.dumps({"id": "FILE", "name": "제목.mp4"}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport, chunk_bytes=4)

    result = client.upload(video, name="제목.mp4", parent="FOLDER", file_id=None)

    assert result["id"] == "FILE"
    assert result["size"] == 10
    assert result["link"] == "https://drive.google.com/file/d/FILE/view"
    puts = [c for c in transport.calls if c["method"] == "PUT"]
    assert [c["headers"]["Content-Range"] for c in puts] == [
        "bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10",
    ]
    assert b"".join(c["body"] for c in puts) == b"0123456789"


def test_an_existing_file_id_updates_in_place(drive_env, tmp_path):
    """재실행이 드라이브에 두 벌을 만들지 않는다 — PATCH로 같은 id를 갱신한다."""
    video = tmp_path / "timeline.ko.mp4"
    video.write_bytes(b"abc")
    transport = FakeTransport([
        _token(),
        (200, {"Location": "https://session/2"}, b""),
        (200, {}, json.dumps({"id": "KEEP"}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport)
    result = client.upload(video, name="제목.mp4", parent="FOLDER", file_id="KEEP")
    session = transport.calls[1]
    assert session["method"] == "PATCH"
    assert "/KEEP?" in session["url"]
    assert "parents" not in json.loads(session["body"])
    assert result["id"] == "KEEP"


def test_a_refused_chunk_becomes_a_drive_error(drive_env, tmp_path):
    video = tmp_path / "timeline.ko.mp4"
    video.write_bytes(b"abc")
    transport = FakeTransport([
        _token(),
        (200, {"Location": "https://session/3"}, b""),
        (403, {}, b"quota exceeded"),
    ])
    client = gdrive.DriveClient(transport=transport)
    with pytest.raises(gdrive.DriveError) as exc:
        client.upload(video, name="제목.mp4", parent="FOLDER")
    assert "403" in str(exc.value)


def test_an_empty_or_missing_file_is_refused_before_any_call(drive_env, tmp_path):
    client = gdrive.DriveClient(transport=FakeTransport([]))
    with pytest.raises(gdrive.DriveError):
        client.upload(tmp_path / "nope.mp4", name="a.mp4", parent="F")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(gdrive.DriveError):
        client.upload(empty, name="a.mp4", parent="F")


# --- 단계: 페이크 클라이언트로 규칙을 잰다 -------------------------------------


class FakeDrive:
    def __init__(self, existing: dict | None = None):
        self.existing = existing or {}
        self.folders: list[tuple[str, str | None]] = []
        self.uploads: list[dict] = []
        self.next_id = 0

    def ensure_folder(self, name, parent):
        self.folders.append((name, parent))
        return "FOLDER"

    def find_file(self, name, parent):
        return self.existing.get(name)

    def upload(self, path: Path, *, name, parent, file_id=None):
        self.next_id += 1
        new_id = file_id or f"ID{self.next_id}"
        self.uploads.append(
            {"path": path, "name": name, "parent": parent, "file_id": file_id}
        )
        return {
            "id": new_id, "name": name, "size": path.stat().st_size,
            "link": gdrive.file_link(new_id),
        }


TITLES = {"ko": KO_TITLE, "ja": JA_TITLE, "en": EN_TITLE}


def _make_run(paths, run_id="20260830-cochineal-red", langs=("ko", "ja", "en"),
              titles=None) -> Path:
    titles = TITLES if titles is None else titles
    run_dir = paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        (run_dir / f"timeline.{lang}.mp4").write_bytes(b"video-" + lang.encode())
        document = {
            "run_id": run_id,
            "topic": "코치닐",
            "total_duration": 60.5,
            "scenes": [],
        }
        if titles.get(lang):
            document["title"] = titles[lang]
        (run_dir / f"scenes.timed.{lang}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
    return run_dir


def test_the_stage_names_the_folder_by_upload_day_and_files_by_title(paths, drive_env):
    run_dir = _make_run(paths)
    drive = FakeDrive()

    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=drive, today=date(2026, 8, 31),
    )

    assert drive.folders == [(f"2026-08-31 {KO_TITLE}", None)]
    assert [u["name"] for u in drive.uploads] == [
        f"{KO_TITLE}.mp4", f"{JA_TITLE}.mp4", f"{EN_TITLE}.mp4",
    ]
    record = json.loads((run_dir / upload_stage.UPLOAD_FILE).read_text(encoding="utf-8"))
    assert record["folder"]["id"] == "FOLDER"
    assert record["languages"]["ko"]["id"] == "ID1"
    assert result.languages["en"].link.endswith("/ID3/view")


def test_the_parent_folder_env_is_used_when_set(paths, drive_env, monkeypatch):
    _make_run(paths)
    monkeypatch.setenv(gdrive.PARENT_ENV, "PARENT")
    drive = FakeDrive()
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=drive, today=date(2026, 8, 31),
    )
    assert drive.folders[0][1] == "PARENT"


def test_a_second_run_updates_the_same_file_instead_of_making_a_new_one(paths, drive_env):
    run_dir = _make_run(paths)
    first = FakeDrive()
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=first, today=date(2026, 8, 31),
    )
    # 영상이 다시 조립됐다 — 크기가 바뀌면 다시 올라가되 **같은 id**로 간다.
    (run_dir / "timeline.ko.mp4").write_bytes(b"video-ko-longer")
    second = FakeDrive()
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=second, today=date(2026, 8, 31),
    )
    assert [u["file_id"] for u in second.uploads] == ["ID1"]


def test_an_unchanged_language_is_not_uploaded_again(paths, drive_env):
    _make_run(paths)
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )
    again = FakeDrive()
    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=again, today=date(2026, 8, 31),
    )
    assert again.uploads == []
    assert all(item.skipped for item in result.languages.values())


def test_force_uploads_even_when_nothing_changed(paths, drive_env):
    _make_run(paths)
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )
    again = FakeDrive()
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=again, force=True,
        today=date(2026, 8, 31),
    )
    assert len(again.uploads) == 3


def test_a_lost_record_reattaches_to_the_same_name_in_the_folder(paths, drive_env):
    """`upload.json`을 잃어도 폴더에 같은 이름이 있으면 그것을 갱신한다 — 두 벌이 되지 않는다."""
    _make_run(paths)
    drive = FakeDrive(existing={f"{KO_TITLE}.mp4": "OLD"})
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=drive, langs=["ko"],
        today=date(2026, 8, 31),
    )
    assert drive.uploads[0]["file_id"] == "OLD"


def test_only_languages_with_a_timeline_run(paths, drive_env):
    """D-3 — ja·en이 없으면 ko만 올라가고 그것이 정상이다."""
    _make_run(paths, langs=("ko",))
    drive = FakeDrive()
    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=drive, today=date(2026, 8, 31),
    )
    assert set(result.languages) == {"ko"}
    assert len(drive.uploads) == 1


def test_a_language_without_a_title_is_skipped_and_the_rest_go_up(paths, drive_env):
    """D-5 — 한 언어의 실패는 그 언어에서 끝난다."""
    titles = dict(TITLES, ja="")
    run_dir = _make_run(paths, titles=titles)
    drive = FakeDrive()

    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=drive, today=date(2026, 8, 31),
    )

    assert result.languages["ja"].error
    assert [u["name"] for u in drive.uploads] == [f"{KO_TITLE}.mp4", f"{EN_TITLE}.mp4"]
    record = json.loads((run_dir / upload_stage.UPLOAD_FILE).read_text(encoding="utf-8"))
    assert record["languages"]["ja"]["error"]
    assert any("ja" in w for w in result.warnings)


def test_a_missing_korean_title_stops_the_stage(paths, drive_env):
    """폴더 이름이 한국어 제목이라 지을 이름이 없다."""
    _make_run(paths, titles=dict(TITLES, ko=""))
    with pytest.raises(upload_stage.UploadStageError) as exc:
        upload_stage.run_upload_stage(
            "20260830-cochineal-red", paths=paths, client=FakeDrive(),
        )
    assert "title" in str(exc.value)


def test_missing_credentials_stop_the_stage_instead_of_skipping(paths, monkeypatch):
    """업로드는 이 단계의 일 전부다 — D-3이 적용되지 않는 자리 (ADR-0084)."""
    _make_run(paths)
    for key in gdrive.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    drive = FakeDrive()
    with pytest.raises(upload_stage.UploadStageError) as exc:
        upload_stage.run_upload_stage(
            "20260830-cochineal-red", paths=paths, client=drive,
        )
    assert "GOOGLE_CLIENT_ID" in str(exc.value)
    assert drive.folders == []


def test_no_timeline_at_all_tells_the_human_to_run_assemble(paths, drive_env):
    run_dir = paths.run_dir("20260830-cochineal-red")
    run_dir.mkdir(parents=True)
    with pytest.raises(upload_stage.UploadStageError) as exc:
        upload_stage.run_upload_stage(
            "20260830-cochineal-red", paths=paths, client=FakeDrive(),
        )
    assert "assemble" in str(exc.value)


def test_the_stage_is_recorded_as_done_in_state(paths, drive_env):
    run_dir = _make_run(paths)
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["stages"][upload_stage.STAGE]["status"] == "done"


# --- 보관 (ADR-0088) ---------------------------------------------------------


def _make_topic(paths, slug="cochineal-red") -> Path:
    topic_dir = paths.topics / slug
    (topic_dir / "judgment").mkdir(parents=True, exist_ok=True)
    (topic_dir / "script.md").write_text("# 대본\n", encoding="utf-8")
    return topic_dir


def test_a_fully_uploaded_topic_moves_to_the_archive(paths, drive_env):
    _make_run(paths)
    topic_dir = _make_topic(paths)

    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )

    assert not topic_dir.exists()
    archived = paths.topics_archive / "cochineal-red"
    assert (archived / "script.md").read_text(encoding="utf-8") == "# 대본\n"
    assert result.archived_to == archived


def test_the_archived_topic_is_still_found_by_topic_dir(paths, drive_env):
    """`[3s]`·`[5]`가 보관된 편의 script.md를 그대로 읽는다 — 폴백이 이 계약이다."""
    _make_run(paths)
    _make_topic(paths)
    upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )

    found = paths.topic_dir("cochineal-red")
    assert found == paths.topics_archive / "cochineal-red"
    assert (found / "script.md").is_file()


def test_a_failed_language_keeps_the_topic_in_the_active_list(paths, drive_env):
    # ja에 title이 없으면 그 언어만 실패한다 (D-5) — 편은 아직 손이 갈 상태다.
    _make_run(paths, titles={"ko": KO_TITLE, "en": EN_TITLE})
    topic_dir = _make_topic(paths)

    result = upload_stage.run_upload_stage(
        "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
    )

    assert topic_dir.exists(), "손이 갈 편은 현역에 남아야 한다"
    assert result.archived_to is None
    assert not (paths.topics_archive / "cochineal-red").exists()


def test_a_second_run_does_not_fail_on_an_already_archived_topic(paths, drive_env):
    _make_run(paths)
    _make_topic(paths)
    for _ in range(2):
        upload_stage.run_upload_stage(
            "20260830-cochineal-red", paths=paths, client=FakeDrive(), today=date(2026, 8, 31),
        )
    assert (paths.topics_archive / "cochineal-red" / "script.md").is_file()


def test_a_brand_new_slug_resolves_to_the_active_place(paths):
    """둘 다 없으면 현역 자리다 — 보관함에 새 토픽이 생기지 않는다."""
    assert paths.topic_dir("never-seen") == paths.topics / "never-seen"


# --- 사람이 드라이브에서 지운 파일 (실측 2026-09-02) ---------------------------


def test_a_deleted_file_is_created_again_instead_of_failing(drive_env, tmp_path):
    """갱신하려던 파일이 없으면(404) **새로 만든다.**

    사람이 드라이브에서 지운 편이 그렇게 된다. 여기서 멈추면 그 편만 영영 안 올라간다 —
    기록에 죽은 id가 남아 다음 실행도 같은 404를 맞기 때문이다 (실측: brooklyn-bridge-wire).
    """
    video = tmp_path / "timeline.ko.mp4"
    video.write_bytes(b"abc")
    gone = json.dumps({"error": {"code": 404, "message": "File not found: DEAD."}}).encode()
    transport = FakeTransport([
        _token(),
        (404, {}, gone),                                    # PATCH — 지워진 파일
        (200, {"Location": "https://session/new"}, b""),    # POST — 새로 만든다
        (200, {}, json.dumps({"id": "NEW"}).encode()),
    ])
    client = gdrive.DriveClient(transport=transport)
    result = client.upload(video, name="제목.mp4", parent="FOLDER", file_id="DEAD")

    patch, post = transport.calls[1], transport.calls[2]
    assert patch["method"] == "PATCH" and "/DEAD?" in patch["url"]
    assert post["method"] == "POST"
    # 새로 만들 때는 부모를 실어야 한다 — 안 그러면 루트에 떨어진다.
    assert json.loads(post["body"])["parents"] == ["FOLDER"]
    assert result["id"] == "NEW"


def test_a_404_without_a_file_id_is_still_a_failure(drive_env, tmp_path):
    """폴더가 없어 나는 404는 새로 만들어도 갈 곳이 없다 — 그대로 실패다."""
    video = tmp_path / "timeline.ko.mp4"
    video.write_bytes(b"abc")
    transport = FakeTransport([
        _token(),
        (404, {}, b'{"error": {"code": 404, "message": "File not found: FOLDER."}}'),
    ])
    client = gdrive.DriveClient(transport=transport)
    with pytest.raises(gdrive.DriveError) as exc:
        client.upload(video, name="제목.mp4", parent="FOLDER")
    assert not isinstance(exc.value, gdrive.DriveGone)


def test_drive_gone_is_a_drive_error(drive_env):
    """이것을 안 잡는 호출부의 동작은 전과 같아야 한다."""
    assert issubclass(gdrive.DriveGone, gdrive.DriveError)
