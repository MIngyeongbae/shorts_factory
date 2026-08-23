"""ADR-0051 — 인물 경로. ADR-0056 이후 **서술 경로만** 산다.

확인 대상:
- `[5]` — `cast` 씬의 SUBJECT에 `appearance`를 싣고 `cast`를 복사한다. 시트 프롬프트는
  없다 — 영상 프로바이더에 참조 입력이 없다 (ADR-0056 되돌릴 조건 6). 블록이 없으면
  산출물이 도입 전과 같다 (D-3)
- 휴면 MJ 어댑터 — 참조 문법(`--oref … --v 7`)과 U1 왕복은 코드가 썩지 않게 계약
  테스트로 남긴다 (ADR-0056 결정 1). 어느 단계도 부르지 않는다

`[6]`의 시트 선행 생성·참조 주입 테스트는 단계와 함께 지웠다 (ADR-0034 §4).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from shorts_factory.imagegen.base import ImageGenError, ImageRequest
from shorts_factory.imagegen.midjourney import build_prompt as build_mj_line

from conftest import HOOVER, load_script

def _request() -> ImageRequest:
    return ImageRequest(
        scene_id=1, prompt="p --ar 9:16", negative_prompt="--no text",
        aspect_ratio="9:16", resolution="2K",
    )


def test_reference_url_is_not_in_digest():
    base = _request()
    signed = replace(base, reference_url="https://cdn.discordapp.com/a.png?ex=1")
    rotated = replace(base, reference_url="https://cdn.discordapp.com/a.png?ex=2")
    assert base.digest == signed.digest == rotated.digest


def test_reference_key_changes_digest():
    base = _request()
    keyed = replace(base, reference_key="wonhyo")
    assert keyed.digest != base.digest


# --- 휴면 MJ 어댑터 — 참조 문법 (G3 실측) --------------------------------------


def test_mj_line_appends_reference_flags():
    url = "https://cdn.discordapp.com/attachments/a/b/c.png?ex=1"
    request = replace(_request(), reference_url=url)
    line = build_mj_line(request)
    # 가중 기본(100)은 시트 구도까지 복제한다 — --ow 25는 G4 프로브 실측이다
    # (ADR-0051 개정 2026-08-22).
    assert line.endswith(f"--oref {url} --ow 25 --v 7")


def test_mj_line_without_reference_is_unchanged():
    assert "--oref" not in build_mj_line(_request())


# --- 휴면 MJ 어댑터 — U1 왕복 (G1 실측: 닿는 주소는 Discord CDN 서명 URL이다) --


DISCORD_URL = "https://cdn.discordapp.com/attachments/a/b/c.png?ex=1&hm=2"


def _mj(script):
    from test_midjourney_client import client

    return client(script)


def test_character_reference_reuses_completed_upscale():
    """같은 업스케일을 짧은 간격에 재요청하면 MJ가 거절한다 (실측) — 완료분을 재사용한다."""
    transport_script = [
        ("/mj/task/list", (200, [
            {"id": "up1", "action": "UPSCALE", "status": "SUCCESS",
             "parentId": "sheet1", "url": DISCORD_URL},
        ])),
    ]
    mj = _mj(transport_script)
    reference = mj.character_reference("sheet1", timeout=5)
    assert reference.upscale_task_id == "up1"
    assert reference.url == DISCORD_URL
    # 재사용이므로 action 제출이 없다.
    assert not any("submit/action" in url for _m, url, _b in mj.transport.calls)


def test_character_reference_submits_u1_and_polls():
    transport_script = [
        ("/mj/task/list", (200, [])),
        ("task/sheet1/fetch", (200, {
            "status": "SUCCESS",
            "buttons": [
                {"customId": "MJ::JOB::upsample::1::uuid", "label": "U1"},
                {"customId": "MJ::JOB::variation::1::uuid", "label": "V1"},
            ],
        })),
        ("submit/action", (200, {"code": 1, "result": "up9"})),
        ("task/up9/fetch", (200, {"status": "SUCCESS", "url": DISCORD_URL})),
    ]
    mj = _mj(transport_script)
    reference = mj.character_reference("sheet1", timeout=5)
    assert reference.upscale_task_id == "up9"
    assert reference.url == DISCORD_URL

    action = next(b for _m, url, b in mj.transport.calls if "submit/action" in url)
    body = json.loads(action)
    assert body == {"taskId": "sheet1", "customId": "MJ::JOB::upsample::1::uuid"}
    # fast 프리픽스다 — 모드는 엔드포인트가 정한다 (ADR-0039 §4).
    assert any("/mj-fast/mj/submit/action" in url for _m, url, _b in mj.transport.calls)


def test_reference_requires_public_url():
    """`imageUrl`(로컬 저장소 주소)로는 안 된다 — MJ 서버가 못 가져간다 (ADR-0046 실측)."""
    from shorts_factory.imagegen.midjourney import MidjourneyClient

    with pytest.raises(ImageGenError, match="닿는 URL"):
        MidjourneyClient._reference_from_task(
            "up1", {"url": None, "imageUrl": "http://localhost:8086/attachments/x.png"}
        )
