"""스토리지 경계 하나 더 — 로컬 파일 하나를 구글 드라이브에 올리고 **파일 id**를 돌려준다 (ADR-0084).

## 왜 SDK를 쓰지 않는가

`storage/r2.py`와 같은 이유다 (ADR-0077). 이 프로젝트의 의존성은 `jsonschema`·`referencing`
**둘**이고 HTTP도 `transport.py`에서 urllib로 직접 친다 (ADR-0021·0043).
`google-api-python-client`는 트리가 수십 MB라 그 결과 어긋난다. 드라이브 REST v3는 평범한
JSON API라 stdlib로 충분하다.

## 왜 OAuth 리프레시 토큰인가

**stdlib로 되는 인증이 이것 하나다.** 서비스 계정은 JWT를 RS256으로 서명해야 하는데 stdlib에
RSA가 없고(그것을 넣으려면 다시 의존성이다), 개인 드라이브에 용량·소유권도 없어 공유
드라이브가 전제가 된다. 리프레시 토큰 교환은 폼 인코딩 POST 한 번이다.

## 왜 resumable인가

타임라인이 **편당 90~120MB**다 (실측: 코치닐 ko 88.6 · ja 118.7 · en 115.4MB). 구글이 단일
요청을 권하는 범위는 5MB 이하이고, 한 요청에 담으면 ① 파일 전체가 메모리에 올라가고
(`Transport`의 본문이 `bytes | None`이다) ② 끊기면 처음부터다. 세션 URI는 응답 `Location`
헤더로, 이어받을 지점은 `Range` 헤더로 오므로 **응답 헤더를 돌려주는 `HeaderTransport`**
(ElevenLabs가 쓰는 그 시그니처)로 transport를 고치지 않고 된다.

## 없으면 없는 대로 돌지 **않는다**

`.env`에 키가 하나라도 없으면 `configured()`가 거짓이고 `[10]`은 **멈춘다.** R2(참조 사진)와
태도가 다른 자리다 — 참조는 그림을 좋게 하는 수단이라 없으면 그냥 그리면 되지만, 업로드는
이 단계의 일 전부다. 조용히 건너뛰면 게시물이 안 올라간 것을 나중에 안다.
"""

from __future__ import annotations

import json
import logging
import unicodedata
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

from ..config import require_env
from ..transport import HeaderTransport, TransportError
from ..transport import urllib_request as _urllib_request

log = logging.getLogger(__name__)

#: `.env`가 들고 있어야 하는 키. 하나라도 비면 `[10]`이 멈춘다.
ENV_KEYS: tuple[str, ...] = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
)

#: 선택 — 없으면 내 드라이브 루트에 폴더를 만든다.
PARENT_ENV = "GDRIVE_PARENT_FOLDER_ID"

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

FOLDER_MIME = "application/vnd.google-apps.folder"
VIDEO_MIME = "video/mp4"

#: 청크 크기. 드라이브는 resumable 청크가 **256KiB의 배수**일 것을 요구한다 (마지막 청크만 예외).
CHUNK_BYTES = 8 * 1024 * 1024

#: 청크 하나가 끊겼을 때 다시 붙는 횟수. 이어받기라 처음부터가 아니다.
CHUNK_RETRIES = 3

#: 드라이브가 파일 이름에서 실제로 못 받는 것은 제어문자다. 나머지(`—`·`/`·따옴표)는 통과한다.
_CONTROL = "Cc"


class DriveError(RuntimeError):
    """올리지 못했다. `[10]`이 잡아 **그 언어만** 실패로 남기고 나머지 언어는 계속한다."""


class DriveGone(DriveError):
    """갱신하려던 파일이 드라이브에 없다 (404).

    실패가 아니라 **새로 만들라는 신호**다 — 사람이 드라이브에서 지웠을 때 그렇게 된다.
    `DriveError`를 상속하므로 이것을 안 잡는 호출부의 동작은 전과 같다.
    """


def configured() -> bool:
    """`.env`에 세 키가 다 있는가."""
    import os

    return all((os.environ.get(key) or "").strip() for key in ENV_KEYS)


def parent_folder_id() -> str | None:
    """부모 폴더 id (`GDRIVE_PARENT_FOLDER_ID`). 없으면 `None` — 내 드라이브 루트다."""
    import os

    return (os.environ.get(PARENT_ENV) or "").strip() or None


def safe_name(title: str) -> str:
    """드라이브에 쓸 이름 — **제어문자만 턴다.**

    제목은 사람이 드라이브에서 읽을 이름이라 줄이거나 로마자로 바꾸지 않는다 (ADR-0084).
    `—`·`,`·따옴표·한자·가나는 드라이브가 그대로 받는다. 줄바꿈·탭 같은 제어문자와 앞뒤
    공백만 정리하고, 그러고도 비면 이름을 지어내지 않고 실패한다 — 제목이 없는 편은 없다.
    """
    cleaned = "".join(" " if unicodedata.category(ch) == _CONTROL else ch for ch in title)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise DriveError("제목이 비어 있다 — 대본의 `#` 줄을 읽지 못했다")
    return cleaned


def folder_name(day: date, korean_title: str) -> str:
    """편 폴더의 이름 — `{올리는 날 YYYY-MM-DD} {한국어 제목}` (ADR-0084 결정 1)."""
    return f"{day.isoformat()} {safe_name(korean_title)}"


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def escape_query(value: str) -> str:
    """드라이브 `q` 문자열의 리터럴 이스케이프 — `\\`와 `'` 둘뿐이다.

    제목에 아포스트로피가 들어간 편(`Europe's …`)에서 질의가 깨지지 않게 한다.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def content_range(start: int, length: int, total: int) -> str:
    """`Content-Range: bytes {처음}-{끝}/{전체}` — 끝은 **포함**이다."""
    return f"bytes {start}-{start + length - 1}/{total}"


def next_offset(range_header: str, fallback: int) -> int:
    """308 응답의 `Range: bytes=0-{N}` → 다음에 보낼 위치 `N+1`.

    헤더가 없거나 모양이 다르면 우리가 보낸 만큼 갔다고 본다 — 그 값이 틀렸으면 다음 청크가
    거절되고 오류가 뜬다. 조용히 어긋난 채 끝까지 가지 않는다.
    """
    text = (range_header or "").strip()
    if "-" not in text:
        return fallback
    tail = text.rsplit("-", 1)[-1]
    return int(tail) + 1 if tail.isdigit() else fallback


class DriveClient:
    """드라이브 경계. 네트워크는 `transport`로만 만난다 — 테스트가 페이크를 꽂는 자리다."""

    def __init__(
        self,
        *,
        transport: HeaderTransport | None = None,
        timeout: int = 300,
        chunk_bytes: int = CHUNK_BYTES,
    ) -> None:
        self._send: HeaderTransport = transport or _urllib_request
        self.timeout = timeout
        self.chunk_bytes = chunk_bytes
        self._token: str | None = None

    # ---- HTTP 한 겹 -------------------------------------------------------

    def _request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            return self._send(method, url, headers, body, self.timeout)
        except TransportError as exc:
            raise DriveError(f"드라이브에 닿지 못했다: {exc}") from exc

    def _json(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _headers, raw = self._request(method, url, headers, body)
        if status not in (200, 201):
            raise DriveError(f"{method} {url} 가 HTTP {status}로 거절됐다: {_detail(raw)}")
        return _decode(raw)

    def access_token(self) -> str:
        """리프레시 토큰 → 액세스 토큰. 한 번 받아 이 객체가 사는 동안 쓴다."""
        if self._token:
            return self._token
        purpose = "구글 드라이브 업로드 (ADR-0084, [10] upload)"
        form = urllib.parse.urlencode({
            "client_id": require_env("GOOGLE_CLIENT_ID", purpose=purpose),
            "client_secret": require_env("GOOGLE_CLIENT_SECRET", purpose=purpose),
            "refresh_token": require_env("GOOGLE_REFRESH_TOKEN", purpose=purpose),
            "grant_type": "refresh_token",
        }).encode("utf-8")
        status, _headers, raw = self._request(
            "POST", TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"}, form,
        )
        if status != 200:
            raise DriveError(
                f"액세스 토큰을 못 받았다 (HTTP {status}): {_detail(raw)} — "
                "GOOGLE_REFRESH_TOKEN이 만료됐거나 클라이언트 정보가 다르다"
            )
        token = str(_decode(raw).get("access_token") or "")
        if not token:
            raise DriveError("토큰 응답에 access_token이 없다")
        self._token = token
        return token

    # ---- 폴더 ------------------------------------------------------------

    def find_folder(self, name: str, parent: str | None) -> str | None:
        """같은 이름의 폴더 id. 없으면 `None`."""
        clauses = [
            f"name = '{escape_query(name)}'",
            f"mimeType = '{FOLDER_MIME}'",
            "trashed = false",
            f"'{escape_query(parent or 'root')}' in parents",
        ]
        query = urllib.parse.urlencode({
            "q": " and ".join(clauses),
            "fields": "files(id,name)",
            "pageSize": "10",
            "spaces": "drive",
        })
        files = self._json("GET", f"{FILES_API}?{query}").get("files") or []
        return str(files[0]["id"]) if files else None

    def ensure_folder(self, name: str, parent: str | None) -> str:
        """폴더 id — **있으면 쓰고 없으면 만든다.** 하루에 두 번 돌려도 폴더가 갈리지 않는다."""
        found = self.find_folder(name, parent)
        if found:
            return found
        payload: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            payload["parents"] = [parent]
        created = self._json("POST", f"{FILES_API}?fields=id", payload)
        return str(created["id"])

    def find_file(self, name: str, parent: str) -> str | None:
        """폴더 안 같은 이름 파일의 id. `upload.json`을 잃었을 때 두 벌이 되지 않게 한다."""
        query = urllib.parse.urlencode({
            "q": (
                f"name = '{escape_query(name)}' and trashed = false "
                f"and '{escape_query(parent)}' in parents"
            ),
            "fields": "files(id,name)",
            "pageSize": "10",
            "spaces": "drive",
        })
        files = self._json("GET", f"{FILES_API}?{query}").get("files") or []
        return str(files[0]["id"]) if files else None

    # ---- 업로드 ----------------------------------------------------------

    def upload(
        self, path: Path, *, name: str, parent: str, file_id: str | None = None
    ) -> dict[str, Any]:
        """파일 하나를 올린다 — `file_id`가 있으면 **그 파일을 갱신한다** (새로 만들지 않는다).

        돌려주는 것은 `{"id", "name", "size", "link"}`다.
        """
        if not path.is_file():
            raise DriveError(f"올릴 파일이 없다: {path}")
        total = path.stat().st_size
        if total <= 0:
            raise DriveError(f"빈 파일은 올리지 않는다: {path}")

        try:
            session = self._open_session(
                path, name=name, parent=parent, file_id=file_id, total=total
            )
        except DriveGone:
            # 갱신하려던 파일이 드라이브에 없다 — 사람이 지운 것이다. 새로 만든다.
            # 여기서 멈추면 **그 편만 영영 안 올라간다**: 기록에 죽은 id가 남아 다음
            # 실행도 같은 404를 맞는다 (실측 2026-09-02, brooklyn-bridge-wire).
            log.info("갱신할 파일이 없어 새로 만든다 (%s) — 드라이브에서 지워진 것이다", name)
            file_id = None
            session = self._open_session(
                path, name=name, parent=parent, file_id=None, total=total
            )
        info = self._send_chunks(path, session=session, total=total)
        return {
            "id": str(info.get("id") or file_id or ""),
            "name": str(info.get("name") or name),
            "size": total,
            "link": file_link(str(info.get("id") or file_id or "")),
        }

    def _open_session(
        self, path: Path, *, name: str, parent: str, file_id: str | None, total: int
    ) -> str:
        """resumable 세션 URI (`Location` 헤더)."""
        fields = "fields=id,name"
        if file_id:
            method, url = "PATCH", f"{UPLOAD_API}/{file_id}?uploadType=resumable&{fields}"
            metadata: dict[str, Any] = {"name": name}
        else:
            method, url = "POST", f"{UPLOAD_API}?uploadType=resumable&{fields}"
            metadata = {"name": name, "parents": [parent], "mimeType": VIDEO_MIME}
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": VIDEO_MIME,
            "X-Upload-Content-Length": str(total),
        }
        body = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        status, response_headers, raw = self._request(method, url, headers, body)
        if status == 404 and file_id:
            # 갱신 대상이 사라진 경우만 갈라낸다 — 폴더(parent)가 없어 나는 404는
            # 새로 만들어도 갈 곳이 없으므로 그대로 실패다.
            raise DriveGone(
                f"갱신할 파일이 드라이브에 없다 ({path.name}, id={file_id}): {_detail(raw)}"
            )
        if status not in (200, 201):
            raise DriveError(
                f"업로드 세션이 HTTP {status}로 거절됐다 ({path.name}): {_detail(raw)}"
            )
        location = _header(response_headers, "location")
        if not location:
            raise DriveError(f"업로드 세션 응답에 Location이 없다 ({path.name})")
        return location

    def _send_chunks(self, path: Path, *, session: str, total: int) -> dict[str, Any]:
        """청크를 순서대로 밀어 넣는다. 308이면 계속, 200/201이면 끝난 것이다."""
        offset = 0
        attempts = 0
        with path.open("rb") as handle:
            while offset < total:
                handle.seek(offset)
                chunk = handle.read(self.chunk_bytes)
                if not chunk:
                    raise DriveError(f"{path.name}: {offset}바이트에서 읽을 것이 없다")
                headers = {
                    "Authorization": f"Bearer {self.access_token()}",
                    "Content-Type": VIDEO_MIME,
                    "Content-Range": content_range(offset, len(chunk), total),
                }
                try:
                    status, response_headers, raw = self._request(
                        "PUT", session, headers, chunk
                    )
                except DriveError:
                    attempts += 1
                    if attempts > CHUNK_RETRIES:
                        raise
                    offset = self._resume_offset(session, total, fallback=offset)
                    continue
                if status in (200, 201):
                    return _decode(raw)
                if status == 308:
                    attempts = 0
                    offset = next_offset(
                        _header(response_headers, "range"), offset + len(chunk)
                    )
                    continue
                if status in (500, 502, 503, 504):
                    attempts += 1
                    if attempts > CHUNK_RETRIES:
                        raise DriveError(
                            f"{path.name}: 청크가 HTTP {status}로 {CHUNK_RETRIES}번 거절됐다"
                        )
                    offset = self._resume_offset(session, total, fallback=offset)
                    continue
                raise DriveError(
                    f"{path.name}: 청크가 HTTP {status}로 거절됐다: {_detail(raw)}"
                )
        raise DriveError(f"{path.name}: 마지막 청크가 완료 응답을 주지 않았다")

    def _resume_offset(self, session: str, total: int, *, fallback: int) -> int:
        """세션에 "어디까지 받았나"를 묻는다 — 본문 없는 `Content-Range: bytes */{전체}`."""
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Range": f"bytes */{total}",
        }
        status, response_headers, _raw = self._request("PUT", session, headers, b"")
        if status in (200, 201):
            return total
        if status != 308:
            return fallback
        return next_offset(_header(response_headers, "range"), 0)


def _header(headers: dict[str, str], name: str) -> str:
    """헤더 이름은 대소문자를 가리지 않는다 — urllib이 준 dict는 가린다."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return str(value)
    return ""


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveError(f"드라이브 응답을 못 읽었다 ({exc})") from exc
    return loaded if isinstance(loaded, dict) else {}


def _detail(raw: bytes) -> str:
    return raw[:300].decode("utf-8", "replace") if raw else ""
