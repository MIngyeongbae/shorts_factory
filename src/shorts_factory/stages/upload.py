"""[10. upload] — 조립된 타임라인을 구글 드라이브로. **파이프라인의 마지막 단계다** (ADR-0084).

specs/05-pipeline.md:

    [10. upload] → upload.json (조립된 타임라인을 구글 드라이브로. 폴더는
                   `{올리는 날} {한국어 제목}`, 파일명은 그 언어 대본의 제목)

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/timeline.{lang}.mp4` | `[9]`가 만든 그 언어의 영상. **올릴 물건이다** |
| `runs/{run_id}/scenes.timed.{lang}.json` | 선택 필드 `title` — 그 언어의 제목 (ADR-0065) |
| `runs/{run_id}/upload.json` | **선택.** 지난 실행의 파일 id — 있으면 갱신하고 새로 만들지 않는다 |

**올릴 것을 정하려고 `topics/`를 읽지는 않는다** — 제목은 `[3]`이 이미
`scenes.timed.{lang}.json`에 옮겨 뒀다 (ADR-0065). `topics/`를 건드리는 것은 아래
「올린 편은 보관함으로 간다」 하나뿐이고, 그것은 입력이 아니라 뒷정리다.

## 올린 편은 보관함으로 간다 (ADR-0088)

모든 언어가 성공하면 `topics/{slug}`를 `topics/_archive/{slug}`로 옮긴다 — 사람이
`topics/`를 열었을 때 **다음에 할 일만 보이게** 하려는 것이다. 실패한 언어가 하나라도
있으면 옮기지 않는다 (아직 손이 갈 편이다).

**옮기기는 업로드의 조건이 아니라 뒷정리다** — 실패해도 경고만 남기고 단계는 성공이다.
파일은 이미 드라이브에 올라갔으므로 여기서 예외를 올리면 올라간 편이 실패로 기록된다.
`Paths.topic_dir`이 두 자리를 보므로 보관된 편도 `[3s]`·`[5]` 재실행이 그대로 된다.

## 이름 규칙 (ADR-0084 결정 1·2)

- 폴더: `{올리는 날 YYYY-MM-DD} {한국어 제목}` — 같은 이름이 이미 있으면 그것을 쓴다
- 파일: `{그 언어의 제목}.mp4`

## 설정이 없으면 **멈춘다**

`.env`의 세 키(`GOOGLE_CLIENT_ID`·`GOOGLE_CLIENT_SECRET`·`GOOGLE_REFRESH_TOKEN`)가 없으면
오류다. D-3(선택적 입력의 부재는 경고가 아니다)이 적용되지 않는 자리다 — 업로드는 이 단계의
일 **전부**라, 조용히 건너뛰면 게시물이 안 올라간 것을 나중에 안다.

## 언어는 각자 끝난다 (D-3·D-5)

타임라인이 있는 언어만 돈다. 한 언어가 실패해도 나머지는 올라가고, 실패한 언어의 사유가
`upload.json`에 남는다. 기록은 **언어 하나가 끝날 때마다** 쓴다 — 중간에 죽어도 올라간 것은
기록에 남아 다음 실행이 다시 올리지 않는다 (ADR-0020의 목적).
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from ..config import Paths, write_text
from ..judgment import JudgmentError, languages_for, read_gate, slug_from_run_id
from ..runstate import RunState
from ..schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE
from ..storage import gdrive
from ..storage.gdrive import DriveClient, DriveError
from ..video.thumbnail import THUMBNAIL_PATTERN  # 값의 출처는 하나다 (ADR-0034)
from .assemble import TIMELINE_PATTERN  # 값의 출처는 하나다 (ADR-0034)

log = logging.getLogger(__name__)

STAGE = "10-upload"
UPLOAD_FILE = "upload.json"
TIMED_PATTERN = "scenes.timed.{lang}.json"


class UploadStageError(Exception):
    """단계를 세우는 실패 — 설정 부재, ko 부재, 폴더를 못 만든 경우."""


@dataclass
class LanguageUpload:
    lang: str
    title: str = ""
    file_name: str = ""
    file_id: str = ""
    link: str = ""
    size: int = 0
    skipped: bool = False
    error: str = ""
    #: 썸네일은 **곁다리다** (ADR-0091 결정 5). 없으면 비어 있고 그것이 정상이며(D-3),
    #: 올리다 실패해도 그 편의 업로드는 성공이다 — 영상이 올라간 것이 이 단계의 일이다.
    thumbnail_name: str = ""
    thumbnail_id: str = ""
    thumbnail_link: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.file_id) and not self.error


@dataclass
class UploadResult:
    run_id: str
    run_dir: Path
    topic: str
    folder_name: str = ""
    folder_id: str = ""
    archived_to: Path | None = None
    languages: dict[str, LanguageUpload] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def uploaded(self) -> list[LanguageUpload]:
        return [item for item in self.languages.values() if item.ok and not item.skipped]

    @property
    def summary(self) -> str:
        parts = []
        for lang in LANGUAGES:
            item = self.languages.get(lang)
            if item is None:
                continue
            if item.error:
                parts.append(f"{lang} 실패({item.error})")
            elif item.skipped:
                parts.append(f"{lang} 그대로")
            else:
                parts.append(f"{lang} {item.size / 1048576:.1f}MB")
        total = sum(item.size for item in self.uploaded)
        tail = " / 보관함으로" if self.archived_to else ""
        return (
            f"[10] {self.topic} — 드라이브 '{self.folder_name}' "
            f"({' · '.join(parts) if parts else '올린 것 없음'}) "
            f"/ 올린 용량 {total / 1048576:.1f}MB{tail} → {UPLOAD_FILE}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def present_timelines(run_dir: Path) -> list[str]:
    """`[9]`가 만들어 둔 언어 — 이 단계의 입력은 실측이 아니라 **영상**이다."""
    return [
        lang for lang in LANGUAGES
        if (run_dir / TIMELINE_PATTERN.format(lang=lang)).is_file()
    ]


def title_of(run_dir: Path, lang: str) -> str:
    """그 언어의 제목 (`scenes.timed.{lang}.json`의 `title`, ADR-0065). 없으면 빈 문자열."""
    path = run_dir / TIMED_PATTERN.format(lang=lang)
    if not path.is_file():
        return ""
    document = _load_json(path, f"[3]의 산출물({path.name})")
    return str(document.get("title") or "").strip()


def load_record(run_dir: Path) -> dict[str, Any]:
    """지난 실행의 `upload.json`. 없으면 빈 기록 — 첫 실행이다 (D-3)."""
    path = run_dir / UPLOAD_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 기록이 깨졌으면 없는 셈 친다 — 드라이브에서 같은 이름을 찾아 다시 잇는다.
        return {}


def _write_record(run_dir: Path, document: dict[str, Any]) -> None:
    write_text(run_dir / UPLOAD_FILE, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _unchanged(previous: dict[str, Any], path: Path) -> bool:
    """지난번에 올린 그 파일 그대로인가 — 크기와 수정 시각이 둘 다 같아야 한다."""
    if not previous.get("id"):
        return False
    stat = path.stat()
    return (
        int(previous.get("size") or -1) == stat.st_size
        and int(previous.get("source_mtime") or -1) == int(stat.st_mtime)
    )


def run_upload_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    langs: Sequence[str] | None = None,
    force: bool = False,
    client: DriveClient | None = None,
    today: date | None = None,
) -> UploadResult:
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    present = present_timelines(run_dir)
    if not present:
        raise UploadStageError(
            f"올릴 타임라인이 없다: {run_dir}. [9. assemble]을 먼저 실행하라"
        )
    # 돌리는 언어는 **명시 > 판정 > 있는 것 전부**다 (ADR-0094 결정 4).
    try:
        wanted = languages_for(paths, run_id, langs=langs, present=present)
    except JudgmentError as exc:
        raise UploadStageError(str(exc)) from exc
    unknown = [lang for lang in wanted if lang not in LANGUAGES]
    if unknown:
        raise UploadStageError(f"모르는 언어다: {unknown} (가능: {', '.join(LANGUAGES)})")
    absent = [lang for lang in wanted if lang not in present]
    if absent:
        raise UploadStageError(
            f"타임라인이 없는 언어다: "
            f"{', '.join(TIMELINE_PATTERN.format(lang=l) for l in absent)} — [9]가 그 언어를 돌지 않았다"
        )
    selected = [lang for lang in LANGUAGES if lang in wanted]

    # **폴더 이름은 한국어 제목이 진다** (ADR-0084 결정 1) — ko가 없으면 지을 이름이 없다.
    korean_title = title_of(run_dir, PRIMARY_LANGUAGE)
    if not korean_title:
        raise UploadStageError(
            f"{TIMED_PATTERN.format(lang=PRIMARY_LANGUAGE)}에 title이 없다 — "
            "폴더 이름이 한국어 제목이라 지을 이름이 없다 (ADR-0065·0084)"
        )
    if not gdrive.configured():
        raise UploadStageError(
            "구글 드라이브 설정이 없다 — .env에 "
            f"{' · '.join(gdrive.ENV_KEYS)}를 넣어라 (선택: {gdrive.PARENT_ENV}). "
            "업로드는 이 단계의 일 전부라 조용히 건너뛰지 않는다 (ADR-0084)"
        )

    topic = _load_json(
        run_dir / TIMED_PATTERN.format(lang=PRIMARY_LANGUAGE), "[3]의 산출물"
    ).get("topic") or run_id
    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    result = UploadResult(run_id=run_id, run_dir=run_dir, topic=str(topic))

    previous = load_record(run_dir)
    previous_langs: dict[str, Any] = previous.get("languages") or {}

    drive = client or DriveClient()
    name = gdrive.folder_name(today or date.today(), korean_title)
    result.folder_name = name

    state.mark_running(STAGE)
    try:
        folder_id = drive.ensure_folder(name, gdrive.parent_folder_id())
    except DriveError as exc:
        state.mark_failed(STAGE, str(exc))
        raise UploadStageError(f"드라이브 폴더를 못 만들었다 ('{name}'): {exc}") from exc
    result.folder_id = folder_id
    log.info("[%s] 드라이브 폴더 '%s' (id=%s)", STAGE, name, folder_id)

    document: dict[str, Any] = {
        "run_id": run_id,
        "topic": str(topic),
        "stage": STAGE,
        "folder": {"id": folder_id, "name": name, "link": gdrive.folder_link(folder_id)},
        "languages": dict(previous_langs),
        "warnings": [],
    }

    for lang in selected:
        item = _upload_language(
            lang=lang, run_dir=run_dir, drive=drive, folder_id=folder_id,
            previous=dict(previous_langs.get(lang) or {}), force=force,
            warnings=result.warnings,
        )
        result.languages[lang] = item
        document["languages"][lang] = {
            "title": item.title,
            "file_name": item.file_name,
            "id": item.file_id,
            "link": item.link,
            "size": item.size,
            "source_mtime": int(
                (run_dir / TIMELINE_PATTERN.format(lang=lang)).stat().st_mtime
            ),
            "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": item.error,
            # 썸네일은 곁다리라 이름·id·링크만 남긴다 (ADR-0091). 없으면 빈 값이다.
            "thumbnail_name": item.thumbnail_name,
            "thumbnail_id": item.thumbnail_id,
            "thumbnail_link": item.thumbnail_link,
        }
        document["warnings"] = list(result.warnings)
        # 언어 하나가 끝날 때마다 쓴다 — 중간에 죽어도 올라간 것은 기록에 남는다 (ADR-0020).
        _write_record(run_dir, document)

    failed = [item.lang for item in result.languages.values() if item.error]
    info = {
        "folder": document["folder"],
        "languages": document["languages"],
        "warnings": result.warnings,
    }
    if failed and not result.uploaded:
        state.mark_failed(STAGE, f"모든 언어가 실패했다: {', '.join(failed)}", **info)
        raise UploadStageError(
            f"올린 언어가 하나도 없다 — {', '.join(failed)} "
            f"({result.languages[failed[0]].error})"
        )
    # **올라간 편은 현역 목록에서 뺀다** (ADR-0088). 실패한 언어가 있으면 옮기지 않는다 —
    # 아직 손이 갈 편이 보관함으로 들어가면 사람이 못 찾는다. **판정이 고른 언어가 전부
    # 올라갔을 때만이다** (ADR-0094 결정 5) — 고른 언어의 타임라인이 아직 없으면 미완이다.
    unmet = _unmet_languages(paths, run_id, uploaded=set(document["languages"]))
    if unmet:
        result.warnings.append(
            f"판정이 고른 언어 중 아직 안 올라간 것이 있다: {', '.join(unmet)} — 보관하지 않는다 (ADR-0094)"
        )
    if not failed and not unmet:
        moved = archive_topic(run_id, paths=paths, warnings=result.warnings)
        if moved is not None:
            result.archived_to = moved
            info["archived_to"] = str(moved)
            document["archived_to"] = str(moved)
            _write_record(run_dir, document)

    state.mark_done(STAGE, **info)
    return result


def _unmet_languages(paths: Paths, run_id: str, *, uploaded: set[str]) -> list[str]:
    """판정이 골랐는데 아직 안 올라간 언어. 옛 편(블록 없음)은 빈 목록이다."""
    try:
        gate = read_gate(paths, slug_from_run_id(run_id))
    except JudgmentError:
        return []
    if not gate.present:
        return []
    return [lang for lang in gate.languages if lang not in uploaded]


def archive_topic(
    run_id: str, *, paths: Paths, warnings: list[str]
) -> Path | None:
    """토픽 패키지를 `topics/_archive/{slug}`로 옮긴다 (ADR-0088).

    **옮기기는 업로드의 조건이 아니라 뒷정리다** — 실패해도 경고만 남기고 단계는 성공이다.
    파일은 이미 드라이브에 올라갔으므로 여기서 예외를 올리면 올라간 편이 실패로 기록된다.
    이미 보관함에 있으면 아무것도 하지 않는다 (재실행).
    """
    try:
        slug = slug_from_run_id(run_id)
    except JudgmentError as exc:
        warnings.append(f"보관 건너뜀 — {exc}")
        return None

    source = paths.topics / slug
    target = paths.topics_archive / slug
    if not source.exists():
        return target if target.exists() else None
    if target.exists():
        warnings.append(
            f"보관 건너뜀 — 보관함에 같은 이름이 이미 있다: {target} (현역 폴더는 그대로 둔다)"
        )
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        warnings.append(f"보관 실패 — {source} → {target}: {exc}")
        return None

    log.info("[%s] 토픽을 보관함으로 옮겼다: %s → %s", STAGE, source, target)
    return target


def _upload_language(
    *,
    lang: str,
    run_dir: Path,
    drive: DriveClient,
    folder_id: str,
    previous: dict[str, Any],
    force: bool,
    warnings: list[str],
) -> LanguageUpload:
    """한 언어. **실패는 여기서 끝난다** (D-5) — 사유를 담아 돌려주고 예외를 올리지 않는다."""
    item = LanguageUpload(lang=lang)
    path = run_dir / TIMELINE_PATTERN.format(lang=lang)
    title = title_of(run_dir, lang)
    if not title:
        item.error = f"{TIMED_PATTERN.format(lang=lang)}에 title이 없다"
        warnings.append(f"{lang}: {item.error} — 파일 이름을 지을 수 없어 건너뛴다")
        return item

    item.title = title
    try:
        item.file_name = f"{gdrive.safe_name(title)}.mp4"
    except DriveError as exc:
        item.error = str(exc)
        warnings.append(f"{lang}: {exc}")
        return item

    file_id = str(previous.get("id") or "")
    if file_id and not force and _unchanged(previous, path):
        item.file_id = file_id
        item.link = gdrive.file_link(file_id)
        item.size = int(previous.get("size") or path.stat().st_size)
        item.skipped = True
        log.info("[%s] %s는 지난 실행 그대로라 다시 올리지 않는다", STAGE, lang)
        return item

    try:
        if not file_id:
            # 기록을 잃었어도 같은 이름이 폴더에 있으면 그것을 갱신한다 — 두 벌을 만들지 않는다.
            file_id = drive.find_file(item.file_name, folder_id) or ""
        uploaded = drive.upload(
            path, name=item.file_name, parent=folder_id, file_id=file_id or None
        )
    except DriveError as exc:
        item.error = str(exc)
        warnings.append(f"{lang}: {exc}")
        return item

    item.file_id = str(uploaded["id"])
    item.link = str(uploaded["link"])
    item.size = int(uploaded["size"])
    log.info("[%s] %s → %s (%.1fMB)", STAGE, lang, item.file_name, item.size / 1048576)
    _attach_thumbnail(
        item=item, run_dir=run_dir, drive=drive, folder_id=folder_id,
        previous=previous, warnings=warnings,
    )
    return item


def _attach_thumbnail(
    *,
    item: LanguageUpload,
    run_dir: Path,
    drive: DriveClient,
    folder_id: str,
    previous: dict[str, Any],
    warnings: list[str],
) -> None:
    """`[9t]`가 구운 썸네일이 있으면 영상과 같은 폴더에 올린다 (ADR-0091 결정 5).

    **없으면 아무 일도 안 한다** — `[9t]`를 안 돌렸거나 제목이 없는 언어라 굽지 않은
    것이고, 부재는 경고가 아니다 (D-3). **실패해도 그 언어의 업로드는 성공이다** —
    영상은 이미 올라갔고, 썸네일 때문에 올라간 편을 실패로 기록하면 다음 실행이 영상을
    다시 올린다 (D-5, `gdrive.py`의 `DriveGone`과 같은 태도).

    붙이는 것은 사람이다 — 파이프라인은 드라이브까지이고 스튜디오에서 이 파일을 고르는
    것은 사람 손이다 (ADR-0084의 경계).
    """
    path = run_dir / THUMBNAIL_PATTERN.format(lang=item.lang)
    if not path.exists():
        return
    try:
        name = f"{gdrive.safe_name(item.title)}.png"
        file_id = str(previous.get("thumbnail_id") or "") or (
            drive.find_file(name, folder_id) or ""
        )
        uploaded = drive.upload(
            path, name=name, parent=folder_id, file_id=file_id or None
        )
    except DriveError as exc:
        warnings.append(f"{item.lang}: 썸네일을 못 올렸다 — {exc} (영상은 올라갔다)")
        return

    item.thumbnail_name = name
    item.thumbnail_id = str(uploaded["id"])
    item.thumbnail_link = str(uploaded["link"])
    log.info("[%s] %s 썸네일 → %s", STAGE, item.lang, name)
