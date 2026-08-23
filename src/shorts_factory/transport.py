"""HTTP 경계 하나 — stdlib `urllib`로 요청 한 번을 보내고 `(status, headers, body)`를 돌려준다.

어댑터(`tts/elevenlabs.py`·`videogen/veo.py`·휴면 `imagegen/midjourney.py`)는 SDK를 쓰지
않고 이 함수 하나로 HTTP를 친다 (ADR-0021·0043). 테스트는 같은 시그니처의 페이크를
주입해 실제 호출 없이 전 경로를 검증한다.

## 왜 따로 있는가 (ADR-0056)

`Transport`·`urllib_transport`는 원래 `imagegen/midjourney.py`에 있었고 `videogen/veo.py`가
거기서 import했다. ADR-0056이 MJ 이미지 어댑터를 **휴면**으로 내리면서 살아 있는 코드가
휴면 코드에 기대는 모양이 됐다 — 그래서 공용 모듈로 옮겼다. 휴면 어댑터가 여기에
기대는 것은 괜찮다 (방향이 반대다).

## 오류는 여기 것으로 던진다

`TransportTimeout`·`TransportError`는 프로바이더를 모른다. 어댑터가 자기 예외 계층
(`TTSTimeout`·`VideoGenTimeout`·`ImageGenTimeout`)으로 바꿔 올린다 — 호출부가 보는
예외는 어댑터마다 전과 같다.

오류 응답(4xx·5xx)은 예외가 아니라 `(status, headers, body)`로 돌려준다 — 서버가 본문에
사유를 적어 주므로 어댑터가 그것을 읽고 판정한다.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Callable


class TransportError(Exception):
    """연결 실패 등 응답 자체를 받지 못한 경우."""


class TransportTimeout(TransportError):
    """`timeout`초 안에 응답이 오지 않았다."""


#: `(method, url, headers, body, timeout) -> (status, body)`. 어댑터 대부분이 쓰는 모양이다.
Transport = Callable[[str, str, dict[str, str], bytes | None, int], "tuple[int, bytes]"]

#: `(method, url, headers, body, timeout) -> (status, response_headers, body)`.
#: 응답 헤더까지 필요한 어댑터(ElevenLabs의 `request-id` 추적)가 쓴다.
HeaderTransport = Callable[
    [str, str, dict[str, str], bytes | None, int], "tuple[int, dict[str, str], bytes]"
]


def urllib_request(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int
) -> tuple[int, dict[str, str], bytes]:
    """stdlib 요청. 오류 응답도 본문·헤더를 살려 돌려준다."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except TimeoutError as exc:
        raise TransportTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TransportTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
        raise TransportError(f"연결 실패: {exc.reason}") from exc


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int
) -> tuple[int, bytes]:
    """`urllib_request`에서 응답 헤더를 뺀 모양 — `Transport` 시그니처다."""
    status, _headers, raw = urllib_request(method, url, headers, body, timeout)
    return status, raw
