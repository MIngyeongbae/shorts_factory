"""실물 참조 사진 → MJ `--oref` (ADR-0077).

확인 대상:

- **라이선스와 적합성이 갈린다.** `attachable`은 `[8]`이 사진을 **그대로 싣는** 경로를
  지키고, `reference_ok`는 MJ가 **형태만 참조하는** 경로를 고른다. 서로를 대신하지 않는다
- **참조는 조건이 아니라 수단이다.** 스토리지가 없거나 업로드가 깨져도 그림은 산다
- **키는 내용이 정한다** — 주소가 돌아도 지문이 같아 다시 사지 않는다 (ADR-0051의 자리)
- SigV4 서명이 **결정적**이고 입력이 바뀌면 서명도 바뀐다
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from shorts_factory.stages.frames import pick_reference
from shorts_factory.storage import r2

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _headers(**overrides):
    args = dict(
        host="acct.r2.cloudflarestorage.com",
        object_key="bucket/refs/abc.png",
        data=PNG,
        access_key="AKIDEXAMPLE",
        secret_key=SECRET,
        content_type="image/png",
        now=NOW,
    )
    args.update(overrides)
    return r2.signed_put_headers(**args)


# --- 어느 사진이 참조가 되는가 -------------------------------------------------


def test_reference_ok_picks_the_photo_not_the_license():
    """라이선스가 막혀도 적합하면 참조가 된다 — 재현과 축이 다르다 (ADR-0077 맥락 5)."""
    scene = {
        "images": [
            {"attachable": False, "reference_ok": True, "file": "refs/3/01.png"},
        ]
    }
    assert pick_reference(scene) == "refs/3/01.png"


def test_an_attachable_photo_is_not_automatically_a_reference():
    """`attachable`은 `[8]`의 게이트다 — 이 경로를 열지 않는다."""
    scene = {"images": [{"attachable": True, "file": "refs/3/01.png"}]}
    assert pick_reference(scene) == ""


def test_a_reference_without_a_downloaded_file_is_skipped():
    """올릴 실물이 없으면 주소를 만들 수 없다."""
    scene = {"images": [{"reference_ok": True, "source_url": "https://x/y.png"}]}
    assert pick_reference(scene) == ""


def test_the_first_usable_photo_wins():
    scene = {
        "images": [
            {"reference_ok": False, "file": "refs/3/01.png"},
            {"reference_ok": True, "file": "refs/3/02.png"},
            {"reference_ok": True, "file": "refs/3/03.png"},
        ]
    }
    assert pick_reference(scene) == "refs/3/02.png"


def test_no_refs_means_no_reference():
    assert pick_reference(None) == ""
    assert pick_reference({}) == ""


# --- 키는 내용이 정한다 --------------------------------------------------------


def test_the_key_comes_from_the_bytes_not_the_name():
    assert r2.content_key(PNG, ".png") == r2.content_key(PNG, ".PNG")
    assert r2.content_key(PNG, ".png") != r2.content_key(PNG + b"x", ".png")
    assert r2.content_key(PNG, ".png").startswith("refs/")


# --- 서명 ---------------------------------------------------------------------


def test_the_signature_is_deterministic():
    assert _headers()["Authorization"] == _headers()["Authorization"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("object_key", "bucket/refs/other.png"),
        ("data", PNG + b"tail"),
        ("secret_key", SECRET.replace("w", "W", 1)),
        ("now", datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)),
    ],
)
def test_changing_any_signed_input_changes_the_signature(field, value):
    assert _headers()["Authorization"] != _headers(**{field: value})["Authorization"]


def test_the_payload_hash_is_the_body_hash():
    import hashlib

    assert _headers()["x-amz-content-sha256"] == hashlib.sha256(PNG).hexdigest()


def test_content_type_follows_the_suffix():
    assert r2.content_type_of(Path("a.PNG")) == "image/png"
    assert r2.content_type_of(Path("a.jpeg")) == "image/jpeg"
    assert r2.content_type_of(Path("a.bin")) == r2.DEFAULT_CONTENT_TYPE


# --- 실패해도 그림은 산다 ------------------------------------------------------


def test_a_missing_file_is_an_object_store_error_not_a_crash(tmp_path):
    with pytest.raises(r2.ObjectStoreError):
        r2.put_file(tmp_path / "nope.png")


def test_an_empty_file_is_refused(tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(r2.ObjectStoreError):
        r2.put_file(empty)


def test_a_rejected_upload_raises_object_store_error(tmp_path, monkeypatch):
    photo = tmp_path / "ref.png"
    photo.write_bytes(PNG)
    for key in r2.ENV_KEYS:
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub.example")

    def refuse(method, url, headers, body, timeout):
        return 403, b"denied"

    with pytest.raises(r2.ObjectStoreError) as exc:
        r2.put_file(photo, transport=refuse)
    assert "403" in str(exc.value)


def test_a_successful_upload_returns_the_public_url(tmp_path, monkeypatch):
    photo = tmp_path / "ref.png"
    photo.write_bytes(PNG)
    for key in r2.ENV_KEYS:
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub.example/")
    sent: dict = {}

    def accept(method, url, headers, body, timeout):
        sent.update(method=method, url=url, headers=headers, body=body)
        return 200, b""

    url = r2.put_file(photo, transport=accept)
    assert url == f"https://pub.example/{r2.content_key(PNG, '.png')}"
    assert sent["method"] == "PUT"
    assert sent["body"] == PNG
    assert sent["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
