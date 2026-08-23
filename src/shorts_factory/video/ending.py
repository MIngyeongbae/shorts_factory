"""엔딩 실사 컷 — 사진 한 장 + 크레딧 → 클립 하나의 FFmpeg 필터. ADR-0055.

specs/03 「엔딩 실사」를 그대로 옮긴 것이다. `timeline.py`·`subtitles.py`와 같은 자리 —
여기는 전부 순수 함수이고 프로세스는 `ffmpeg.run_ffmpeg`가 부른다.

## 크롭도 줌도 없다 — 무수정 정지 표시

옛 켄번스 클립(ADR-0056이 삭제)은 원본을 9:16으로 **자르고** 그 안에서 창을 움직였다.
여기서는 둘 다 하지 않는다. 근거가 취향이 아니라 라이선스다 (specs/03 「엔딩 실사」):

    `cc-by-sa` 사진을 영상에 실을 수 있는 근거가 "개작하지 않았다" 위에 서 있고,
    크롭과 zoompan은 그 축을 건드린다.

그래서 체인이 이렇게 된다.

    -loop 1 -framerate 30 -i {photo}
      format=gbrp                     → 스케일을 풀 크로마에서 한다 (크로마 서브샘플링 제거)
      scale=1080:1920:decrease        → 원비율 유지. 잘리지 않는다
      pad=1080:1920:중앙:color        → 남는 자리는 단색. 블러 배경도 파생물이라 안 쓴다
      setsar=1
      [drawtext=textfile=...]         → 크레딧 (표시 의무 이행)
    -frames:v N

## 크레딧은 파일로 넘긴다

`drawtext`의 `text=`에 한국어와 문장부호를 직접 넣으면 `:`·`'`·`,`가 필터 인자
구분자와 부딪힌다. `textfile=`로 넘기면 escape가 필요한 것이 경로 하나로 줄고, 화면에
구운 문자열이 그대로 파일로 남아 사람이 확인할 수 있다.

**폰트 파일이 없으면 굽지 않는다.** 강등 사다리는 `굽기+기록 → 기록`이고
(specs/05 `[8]`), `ending/credits.txt`는 어느 쪽이든 남는다 — 표시 의무를 지키는 수단이
화면 하나뿐이면 폰트 자산이 빠진 체크아웃에서 조용히 의무를 어기게 된다.
"""

from __future__ import annotations

import math

from ..schemas import ending as ending_schema
from .ffmpeg import FPS, HEIGHT, PIXEL_FORMAT, VIDEO_CODEC, WIDTH

#: 사진이 못 채운 자리를 메우는 색. 검정에 가까운 중성 회색이라 사진 가장자리가
#: 화면 끝과 구별되고, 자막 밴드(specs/03)와도 다투지 않는다.
PAD_COLOR = "0x101010"

#: 중간 산출물의 CRF. `[9]`에서 한 번 더 인코딩되므로 낮게 잡는다 (중간본은 무손실에 가깝게).
CLIP_CRF = 14
CLIP_PRESET = "medium"

#: zoompan을 타지 않지만 스케일 전에 크로마 서브샘플링을 없앤다 — 사진을 줄이는
#: 연산이라 풀 크로마에서 하는 편이 가장자리 색 번짐이 적다.
WORKING_FORMAT = "gbrp"

#: 크레딧 글자. 자막(40px)보다 작다 — 읽히되 그림을 가리지 않아야 한다.
CREDIT_FONT_SIZE = 26
CREDIT_MARGIN_V = 48
CREDIT_BORDER = 2


class EndingRenderError(Exception):
    """엔딩 컷을 필터로 옮길 수 없음."""


def frame_count(length: float, *, fps: int = FPS) -> int:
    """클립 길이(초) → 프레임 수. **올림이다** — `[9]`의 xfade가 꼬리 끝까지 그림을
    요구하고, 넘치는 몫은 `trim`이 자른다."""
    if length <= 0:
        raise EndingRenderError(f"클립 길이는 0보다 커야 한다: {length}")
    return max(1, math.ceil(round(length * fps, 6)))


def fit_filter(*, width: int = WIDTH, height: int = HEIGHT, color: str = PAD_COLOR) -> str:
    """사진을 **자르지 않고** 화면에 앉힌다. specs/03 「엔딩 실사」의 무수정 표시."""
    return (
        f"format={WORKING_FORMAT},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color},"
        "setsar=1"
    )


def credit_filter(
    textfile: str,
    fontfile: str,
    *,
    font_size: int = CREDIT_FONT_SIZE,
    margin_v: int = CREDIT_MARGIN_V,
) -> str:
    """크레딧 한 줄을 화면 하단 중앙에 굽는다.

    `textfile`·`fontfile`은 **이미 escape된** 경로 문자열이다 (`ffmpeg.escape_filter_path`).
    흰 글자 + 검정 테두리는 자막과 같은 근거다 — 배경이 무엇일지 모르므로 가독성을
    배경이 아니라 글자가 진다 (specs/03).

    자리는 `h`(입력 높이)로 잡는다. 해상도를 숫자로 적으면 `pad`가 만든 실제 프레임과
    갈릴 수 있고, 이 필터는 이미 규격에 맞춰진 프레임 위에서 돈다.
    """
    if not textfile or not fontfile:
        raise EndingRenderError("크레딧을 구우려면 텍스트 파일과 폰트 파일이 둘 다 필요하다")
    return (
        f"drawtext=textfile={textfile}:fontfile={fontfile}"
        f":fontsize={font_size}:fontcolor=white"
        f":borderw={CREDIT_BORDER}:bordercolor=black@0.85"
        f":x=(w-text_w)/2:y=h-text_h-{margin_v}"
    )


def build_filter(
    *,
    credit_textfile: str | None = None,
    fontfile: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> str:
    """사진 → 클립 한 개의 `-vf` 문자열. 크레딧은 둘 다 있을 때만 붙는다."""
    chain = fit_filter(width=width, height=height)
    if credit_textfile and fontfile:
        chain += "," + credit_filter(credit_textfile, fontfile)
    return chain


def build_command(
    photo: str,
    output: str,
    *,
    frames: int,
    credit_textfile: str | None = None,
    fontfile: str | None = None,
    executable: str = "ffmpeg",
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    crf: int = CLIP_CRF,
    preset: str = CLIP_PRESET,
) -> list[str]:
    """엔딩 컷 하나를 만드는 FFmpeg 인자 배열.

    씬 클립과 같은 규격(`ffmpeg.py`의 해상도·fps·코덱·픽셀 포맷)으로 낸다 — `[9]`의
    xfade·concat이 두 입력의 해상도·SAR·픽셀 포맷이 같기를 요구하고, 엔딩 컷은 씬 클립과
    직접 이어 붙는다.

    소리는 넣지 않는다(`-an`). 엔딩 구간은 BGM이 덮는다 (`[10. mix]`, ADR-0055).
    """
    return [
        executable, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", photo,
        "-vf", build_filter(
            credit_textfile=credit_textfile, fontfile=fontfile,
            width=width, height=height,
        ),
        "-frames:v", str(frames),
        "-an",
        "-c:v", VIDEO_CODEC,
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", PIXEL_FORMAT,
        "-r", str(fps),
        "-movflags", "+faststart",
        output,
    ]


#: 화면과 `credits.txt`에 쓰는 라이선스 표기. `refs.schema.json`의 enum이 버전을 담지
#: 않으므로(`cc-by-sa`) 여기서도 버전을 지어내지 않는다 — 정확한 판본은 `source_url`이
#: 가리키는 출처 페이지에 있고 그 주소는 `credits.txt`에 그대로 남는다.
#:
#: **값 목록이 아니라 표기 사전이다.** 게시 가능 목록은 `ending.schema.json`이 정하고,
#: 여기 없는 값이 오면 슬러그를 그대로 보여 준다 (`license_label`).
LICENSE_LABELS = {
    "public-domain": "Public Domain",
    "cc0": "CC0",
    "cc-by": "CC BY",
    "cc-by-sa": "CC BY-SA",
}

#: 게시 가능한 라이선스에는 반드시 표기가 있어야 한다 — 없으면 화면에 `cc-by-nd` 같은
#: 슬러그가 그대로 굽힌다. 계약(스키마)과 코드의 표가 갈리면 로드 시점에 터지게 하는
#: 장치다 (ADR-0034 §3).
_unlabeled = set(ending_schema.publishable_licenses()) - set(LICENSE_LABELS)
if _unlabeled:
    raise EndingRenderError(
        f"게시 가능 라이선스에 표기가 없다: {sorted(_unlabeled)}. "
        "ending.schema.json의 publishable_licenses와 LICENSE_LABELS가 갈렸다"
    )


def license_label(license_name: str) -> str:
    """사람이 읽는 라이선스 이름. 모르는 값은 그대로 보여 준다."""
    return LICENSE_LABELS.get(license_name, license_name)


def credit_line(credit: str, license_name: str) -> str:
    """화면에 굽는 한 줄. 표시할 것이 없으면 빈 문자열이다.

    저작자 표시 의무가 있는 사진에서 이 값이 비는 일은 없다 — `[8]`이 그런 사진을
    후보에서 이미 뺐다 (`schemas/ending.credit_ok`).
    """
    name = credit.strip()
    label = license_label(license_name)
    if name and label:
        return f"{name} · {label}"
    return name or label


def credits_document(photos: list[dict]) -> str:
    """업로드 설명란에 붙일 출처 목록. **화면 크레딧이 강등돼도 이 파일은 남는다.**

    화면 한 줄이 담지 못하는 것(출처 주소)까지 여기 적는다 — 표시 의무를 지키는 수단이
    화면 하나뿐이면 폰트가 빠진 체크아웃에서 조용히 의무를 어기게 된다.
    """
    lines = ["# 엔딩 실사 출처 (ADR-0055)", ""]
    for photo in photos:
        name = str(photo.get("credit") or "").strip() or "(저작자 표시 없음)"
        lines.append(f"{photo['index']}. {name} — {license_label(photo['license'])}")
        lines.append(f"   {photo['source_url']}")
        shows = str(photo.get("shows") or "").strip()
        if shows:
            lines.append(f"   ({shows})")
    return "\n".join(lines) + "\n"
