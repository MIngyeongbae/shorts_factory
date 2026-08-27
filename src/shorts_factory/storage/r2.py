"""오브젝트 스토리지 경계 하나 — 로컬 파일 하나를 올리고 **공개 주소**를 돌려준다 (ADR-0077).

## 왜 있는가

MJ의 참조 문법(`--oref {url}`)은 **MJ가 닿을 수 있는 주소**를 요구한다. 우리 참조 사진은
`[4] refpack`이 run 디렉터리에 내려받아 둔 로컬 파일이라 주소가 없다.

원본 주소(`source_url`)를 그대로 주는 길도 있었지만 **같은 토픽에서 이미 깨졌다** —
`zipper-late-adoption`의 `[4]`에서 Wikimedia가 HTTP 429로 3장을 거절했다. 생성 시점에 원본
호스트가 살아 있으리라는 보장이 없고, 죽으면 그 씬만 조용히 참조를 잃는다 (ADR-0077 대안 A).

## 왜 SDK를 쓰지 않는가

이 프로젝트의 의존성은 `jsonschema`·`referencing` **둘뿐**이고 HTTP도 `transport.py`에서
urllib로 직접 친다 (ADR-0021·0043). PUT 하나에 botocore까지 ~50MB를 들이는 것은 그 결과
어긋난다 (ADR-0077 대안 B). R2는 S3 호환이라 **SigV4 서명만 만들면 stdlib로 충분하다** —
서명은 알려진 정답 벡터로 오프라인 검증이 된다.

## 없으면 없는 대로 돈다

`.env`에 키가 하나라도 없으면 `configured()`가 거짓이고, `[6]`은 **참조 없이 그대로 그린다.**
참조는 그림을 좋게 하는 수단이지 조건이 아니다 — 사다리를 막지 않는다 (specs/05 D-3의 태도).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

from ..config import require_env
from ..transport import Transport, TransportError
from ..transport import urllib_transport as _urllib_transport

#: `.env`가 들고 있는 키. 하나라도 비면 이 경로는 꺼진다.
ENV_KEYS: tuple[str, ...] = (
    "R2_ACCOUNT_ID",
    "R2_BUCKET",
    "R2_ACCESS_KEY",
    "R2_SECRET_KEY",
    "R2_PUBLIC_URL",
)

#: R2는 리전이 하나다 (S3 호환 API가 요구하는 자리라 값이 필요할 뿐이다).
REGION = "auto"
SERVICE = "s3"

#: 확장자 → Content-Type. 참조 사진에 실제로 오는 것만 둔다.
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class ObjectStoreError(RuntimeError):
    """올리지 못했다. 호출부는 이것을 잡고 **참조 없이** 계속한다."""


def configured() -> bool:
    """`.env`에 5개 키가 다 있는가. 없으면 참조 경로가 통째로 꺼진다."""
    import os

    return all((os.environ.get(key) or "").strip() for key in ENV_KEYS)


def content_type_of(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), DEFAULT_CONTENT_TYPE)


def content_key(data: bytes, suffix: str) -> str:
    """파일 내용으로 정하는 객체 키.

    **주소가 아니라 내용이 이름을 정한다** — 같은 사진을 두 번 올리면 같은 키가 되어
    덮어쓰기가 무해하고, `[6]`의 지문(`reference_key`)도 같아 다시 사지 않는다
    (ADR-0051이 `reference_key`를 둔 이유와 같은 자리).
    """
    return f"refs/{hashlib.sha256(data).hexdigest()[:32]}{suffix.lower()}"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, stamp: str) -> bytes:
    """SigV4 파생 키 — `AWS4{secret}` → 날짜 → 리전 → 서비스 → `aws4_request`."""
    key = _sign(f"AWS4{secret}".encode("utf-8"), stamp)
    key = _sign(key, REGION)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def signed_put_headers(
    *,
    host: str,
    object_key: str,
    data: bytes,
    access_key: str,
    secret_key: str,
    content_type: str,
    now: datetime,
) -> dict[str, str]:
    """PUT 하나에 필요한 헤더. **순수 함수다** — 네트워크를 모르므로 테스트가 정답 벡터로 잰다.

    서명 대상 헤더는 `host`·`x-amz-content-sha256`·`x-amz-date` 셋으로 고정한다. 적을수록
    서명이 어긋날 자리가 적고, R2가 요구하는 최소가 이 셋이다.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()
    # 키의 `/`는 경로 구분자라 인코딩하지 않는다 — 나머지 문자는 이미 16진수·`.`뿐이다.
    canonical_uri = "/" + object_key
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join(
        ("PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash)
    )
    scope = f"{stamp}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    signature = hmac.new(
        _signing_key(secret_key, stamp), to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def put_file(
    path: Path,
    *,
    transport: Transport | None = None,
    timeout: int = 60,
    now: datetime | None = None,
) -> str:
    """로컬 파일 하나를 올리고 **공개 주소**를 돌려준다.

    같은 내용이면 같은 키라 다시 올려도 덮어쓰기이고 주소가 같다 (`content_key`).
    """
    if not path.is_file():
        raise ObjectStoreError(f"올릴 파일이 없다: {path}")
    data = path.read_bytes()
    if not data:
        raise ObjectStoreError(f"빈 파일은 올리지 않는다: {path}")

    purpose = "MJ 참조 사진을 올릴 오브젝트 스토리지 (ADR-0077)"
    account = require_env("R2_ACCOUNT_ID", purpose=purpose)
    bucket = require_env("R2_BUCKET", purpose=purpose)
    access_key = require_env("R2_ACCESS_KEY", purpose=purpose)
    secret_key = require_env("R2_SECRET_KEY", purpose=purpose)
    public_base = require_env("R2_PUBLIC_URL", purpose=purpose).rstrip("/")

    key = content_key(data, path.suffix)
    host = f"{account}.r2.cloudflarestorage.com"
    object_key = f"{bucket}/{key}"
    headers = signed_put_headers(
        host=host,
        object_key=object_key,
        data=data,
        access_key=access_key,
        secret_key=secret_key,
        content_type=content_type_of(path),
        now=now or datetime.now(timezone.utc),
    )
    send = transport or _urllib_transport
    try:
        status, body = send(
            "PUT", f"https://{host}/{object_key}", headers, data, timeout
        )
    except TransportError as exc:
        raise ObjectStoreError(f"업로드 실패: {exc}") from exc
    if status not in (200, 201):
        detail = body[:200].decode("utf-8", "replace") if body else ""
        raise ObjectStoreError(f"업로드가 HTTP {status}로 거절됐다: {detail}")
    return f"{public_base}/{key}"
