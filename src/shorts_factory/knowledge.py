"""소스 카드 라이브러리 (knowledge/). ADR-0012.

목적은 하나다: **이미 조사한 소스를 다시 찾지 않게 한다.**

- `knowledge/sources/{source_id}.md` — 소스 1건당 카드 1장 (평면 저장)
- `knowledge/index.md`             — 카드 한 줄 요약. 자동 생성, 수동 편집 금지

카드는 파이썬만 쓴다. 세션은 Read로 읽기만 한다 (ADR-0011).
분류 트리·태그·임베딩 검색은 두지 않는다. 탐색은 인덱스 + Read + grep이다.

카드 형식은 `키: 값` frontmatter라 YAML 파서가 필요 없다. 스키마가 평면 8줄뿐이고,
읽고 쓰는 곳이 이 파일 하나이기 때문이다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import write_text

#: 유형 → 신선도(TTL 일수). None이면 만료 없음. specs/06-topic-research.md
FRESHNESS: dict[str, int | None] = {
    "primary": None,     # 실록·승정원일기 등 1차 사료 원문
    "reference": 365,    # 백과·논문·기관 해설·언론
    "status": 90,        # 관람시간·보존공사·집계 등 현황 정보
}
DEFAULT_TYPE = "reference"

CONFIDENCE_LEVELS = ("high", "medium", "low")

#: 세션 산출물 끝에 오는 참조 소스 섹션의 헤딩
CONTRACT_HEADING = "## 참조 소스"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_FACT_RE = re.compile(
    r"^-\s*\[(?P<confidence>\w+)\]\s*(?P<claim>.*?)"
    r"(?:\s+—\s+교차확인:\s*(?P<topics>.*?))?\s*$"
)
_TRACKING_PARAM = re.compile(r"(^|&)utm_[^&]*")


class KnowledgeError(ValueError):
    """카드/계약을 다룰 수 없음."""


# --- source_id ------------------------------------------------------------


def normalize_url(url: str) -> str:
    """같은 문서를 가리키는 URL을 한 형태로 모은다.

    최소한만 한다. 여기서 놓친 표기 차이는 카드가 두 장이 되는 것으로 드러나며,
    그것이 이 기능이 막으려던 바로 그 상황이라 utm 파라미터까지는 거른다.
    """
    text = (url or "").strip()
    if not text:
        raise KnowledgeError("URL이 비어 있다")
    if "://" not in text:
        text = "https://" + text

    parts = urlsplit(text)
    if not parts.netloc:
        raise KnowledgeError(f"호스트를 찾을 수 없는 URL이다: {url!r}")

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parts.path.rstrip("/") or ""
    query = _TRACKING_PARAM.sub("", parts.query).strip("&")

    # http/https는 같은 문서로 본다. fragment는 버린다.
    return urlunsplit(("https", host, path, query, ""))


def make_source_id(url: str) -> str:
    """URL 기반 결정적 id. 같은 URL이면 항상 같은 값이다."""
    normalized = normalize_url(url)
    host = urlsplit(normalized).netloc
    prefix = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def freshness_label(source_type: str) -> str:
    ttl = FRESHNESS.get(source_type, FRESHNESS[DEFAULT_TYPE])
    return "permanent" if ttl is None else f"{ttl}d"


# --- 카드 -----------------------------------------------------------------


@dataclass
class Fact:
    claim: str
    confidence: str = "medium"
    #: 같은 사실을 독립적으로 뽑아낸 토픽 슬러그들
    cross_checked_by: list[str] = field(default_factory=list)


@dataclass
class SourceCard:
    source_id: str
    title: str
    url: str
    source_type: str = DEFAULT_TYPE
    subjects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    first_researched: str = ""
    last_verified: str = ""
    excerpts: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)

    @property
    def freshness(self) -> str:
        return freshness_label(self.source_type)

    def expires_on(self) -> date | None:
        ttl = FRESHNESS.get(self.source_type, FRESHNESS[DEFAULT_TYPE])
        if ttl is None or not self.last_verified:
            return None
        try:
            verified = date.fromisoformat(self.last_verified)
        except ValueError:
            return None
        return verified + timedelta(days=ttl)

    def is_stale(self, today: date) -> bool:
        expiry = self.expires_on()
        return expiry is not None and today > expiry


def _join(values: list[str]) -> str:
    return ", ".join(values)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def render_card(card: SourceCard) -> str:
    lines = [
        "---",
        f"source_id: {card.source_id}",
        f"title: {card.title}",
        f"url: {card.url}",
        f"type: {card.source_type}",
        f"subjects: {_join(card.subjects)}",
        f"topics: {_join(card.topics)}",
        f"freshness: {card.freshness}",
        f"first_researched: {card.first_researched}",
        f"last_verified: {card.last_verified}",
        "---",
        "",
        "## 핵심 발췌",
        "",
    ]
    for excerpt in card.excerpts:
        lines.append(f"> {excerpt}")
        lines.append("")

    lines += ["## 확인된 사실", ""]
    for fact in card.facts:
        line = f"- [{fact.confidence}] {fact.claim}"
        if fact.cross_checked_by:
            line += f" — 교차확인: {_join(fact.cross_checked_by)}"
        lines.append(line)

    return "\n".join(lines).rstrip() + "\n"


def parse_card(text: str) -> SourceCard:
    if not text.startswith("---"):
        raise KnowledgeError("카드가 frontmatter로 시작하지 않는다")

    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---")
    if not sep:
        raise KnowledgeError("frontmatter가 닫히지 않았다")

    meta: dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip():
            continue
        key, delim, value = line.partition(":")
        if delim:
            meta[key.strip()] = value.strip()

    if not meta.get("source_id") or not meta.get("url"):
        raise KnowledgeError("카드에 source_id 또는 url이 없다")

    excerpts: list[str] = []
    facts: list[Fact] = []
    section = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        if section == "핵심 발췌" and stripped.startswith(">"):
            excerpts.append(stripped[1:].strip())
        elif section == "확인된 사실" and stripped.startswith("-"):
            match = _FACT_RE.match(stripped)
            if not match:
                continue
            facts.append(
                Fact(
                    claim=match.group("claim").strip(),
                    confidence=match.group("confidence"),
                    cross_checked_by=_split(match.group("topics") or ""),
                )
            )

    return SourceCard(
        source_id=meta["source_id"],
        title=meta.get("title", ""),
        url=meta["url"],
        source_type=meta.get("type", DEFAULT_TYPE),
        subjects=_split(meta.get("subjects", "")),
        topics=_split(meta.get("topics", "")),
        first_researched=meta.get("first_researched", ""),
        last_verified=meta.get("last_verified", ""),
        excerpts=excerpts,
        facts=facts,
    )


def _norm_claim(claim: str) -> str:
    """같은 사실인지 비교하기 위한 정규화. 공백 차이만 흡수한다."""
    return re.sub(r"\s+", " ", claim).strip()


def merge_card(existing: SourceCard, incoming: SourceCard, *, today: date) -> SourceCard:
    """같은 소스의 새 조사 결과를 기존 카드에 합친다.

    같은 사실을 다른 토픽이 독립적으로 뽑았으면 그것이 교차 확인이다.
    """
    for subject in incoming.subjects:
        if subject not in existing.subjects:
            existing.subjects.append(subject)
    for topic in incoming.topics:
        if topic not in existing.topics:
            existing.topics.append(topic)

    for excerpt in incoming.excerpts:
        if excerpt not in existing.excerpts:
            existing.excerpts.append(excerpt)

    seen = {_norm_claim(f.claim): f for f in existing.facts}
    for fact in incoming.facts:
        match = seen.get(_norm_claim(fact.claim))
        if match is None:
            existing.facts.append(fact)
            seen[_norm_claim(fact.claim)] = fact
            continue
        for topic in fact.cross_checked_by:
            if topic not in match.cross_checked_by:
                match.cross_checked_by.append(topic)

    if incoming.title and not existing.title:
        existing.title = incoming.title
    existing.last_verified = today.isoformat()
    return existing


# --- 세션 계약: `## 참조 소스` --------------------------------------------


def extract_contract(text: str) -> dict | None:
    """산출물 끝의 `## 참조 소스` JSON을 꺼낸다. 없으면 None."""
    index = text.rfind(CONTRACT_HEADING)
    if index < 0:
        return None

    section = text[index + len(CONTRACT_HEADING):]
    fences = _FENCE_RE.findall(section)
    candidates = [f.strip() for f in fences] or [section.strip()]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def cards_from_contract(
    contract: dict, *, slug: str, today: date
) -> tuple[list[SourceCard], list[str], list[str]]:
    """계약 JSON → (신규/갱신 카드, 재사용 id 목록, 경고).

    계약 위반은 예외가 아니라 경고다. 조사 본문은 유효한데 부록 파싱 때문에
    900초짜리 세션을 버리는 것은 손해다.
    """
    warnings: list[str] = []

    reused = [str(i).strip() for i in contract.get("reused") or [] if str(i).strip()]

    cards: list[SourceCard] = []
    for idx, entry in enumerate(contract.get("new") or []):
        if not isinstance(entry, dict):
            warnings.append(f"참조 소스 new[{idx}]가 객체가 아니다")
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            warnings.append(f"참조 소스 new[{idx}]에 url이 없어 카드를 만들지 않는다")
            continue
        try:
            source_id = make_source_id(url)
        except KnowledgeError as exc:
            warnings.append(f"참조 소스 new[{idx}]: {exc}")
            continue

        source_type = str(entry.get("type") or DEFAULT_TYPE).strip()
        if source_type not in FRESHNESS:
            warnings.append(
                f"참조 소스 new[{idx}]의 type이 알 수 없는 값이다: {source_type!r} "
                f"→ {DEFAULT_TYPE}로 둔다"
            )
            source_type = DEFAULT_TYPE

        facts: list[Fact] = []
        for raw in entry.get("facts") or []:
            if not isinstance(raw, dict):
                continue
            claim = str(raw.get("claim") or "").strip()
            if not claim:
                continue
            confidence = str(raw.get("confidence") or "medium").strip()
            if confidence not in CONFIDENCE_LEVELS:
                confidence = "medium"
            facts.append(Fact(claim=claim, confidence=confidence, cross_checked_by=[slug]))

        excerpts = [
            str(e).strip() for e in entry.get("excerpts") or [] if str(e).strip()
        ]
        subjects = [
            str(s).strip() for s in entry.get("subjects") or [] if str(s).strip()
        ]

        cards.append(
            SourceCard(
                source_id=source_id,
                title=str(entry.get("title") or url).strip(),
                # id는 정규화된 URL에서 뽑지만 카드에는 세션이 실제로 접속한 URL을
                # 그대로 남긴다. 재확인할 때 확실히 열리는 주소가 그쪽이다.
                url=url,
                source_type=source_type,
                subjects=subjects,
                topics=[slug],
                first_researched=today.isoformat(),
                last_verified=today.isoformat(),
                excerpts=excerpts,
                facts=facts,
            )
        )

    return cards, reused, warnings


# --- 저장소 ---------------------------------------------------------------


class KnowledgeStore:
    """`knowledge/` 디렉터리 하나를 다룬다."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def index_path(self) -> Path:
        return self.root / "index.md"

    def card_path(self, source_id: str) -> Path:
        return self.sources_dir / f"{source_id}.md"

    def load_all(self) -> list[SourceCard]:
        if not self.sources_dir.is_dir():
            return []
        cards: list[SourceCard] = []
        for path in sorted(self.sources_dir.glob("*.md")):
            try:
                cards.append(parse_card(path.read_text(encoding="utf-8")))
            except (KnowledgeError, OSError):
                continue  # 손상된 카드가 파이프라인을 세우지는 않는다
        return cards

    def load(self, source_id: str) -> SourceCard | None:
        path = self.card_path(source_id)
        if not path.is_file():
            return None
        try:
            return parse_card(path.read_text(encoding="utf-8"))
        except (KnowledgeError, OSError):
            return None

    def save(self, card: SourceCard) -> None:
        write_text(self.card_path(card.source_id), render_card(card))

    def apply(
        self, contract: dict, *, slug: str, today: date | None = None
    ) -> tuple[int, int, list[str]]:
        """계약을 반영하고 (신규, 갱신, 경고)를 돌려준다."""
        today = today or date.today()
        cards, reused, warnings = cards_from_contract(contract, slug=slug, today=today)

        created = updated = 0
        for card in cards:
            existing = self.load(card.source_id)
            if existing is None:
                self.save(card)
                created += 1
            else:
                self.save(merge_card(existing, card, today=today))
                updated += 1

        for source_id in reused:
            existing = self.load(source_id)
            if existing is None:
                warnings.append(f"재사용했다고 보고된 카드가 없다: {source_id}")
                continue
            if slug not in existing.topics:
                existing.topics.append(slug)
                self.save(existing)
                updated += 1

        return created, updated, warnings

    # --- 인덱스 ----------------------------------------------------------

    def render_index(self, *, today: date | None = None) -> str:
        today = today or date.today()
        cards = self.load_all()
        stale = [c for c in cards if c.is_stale(today)]

        lines = [
            "<!-- 자동 생성. 직접 편집하지 마라. "
            "`python run.py knowledge reindex`로 다시 만든다. -->",
            "# 소스 카드 인덱스",
            "",
            f"총 {len(cards)}건 · 재확인 필요 {len(stale)}건 ({today.isoformat()} 기준)",
            "",
            "| source_id | 제목 | 유형 | 관련 유적 | 신선도 |",
            "|---|---|---|---|---|",
        ]
        for card in cards:
            if card.is_stale(today):
                freshness = "⚠ 재확인 필요"
            elif card.expires_on() is None:
                freshness = "영구"
            else:
                freshness = f"~{card.expires_on().isoformat()}"
            lines.append(
                f"| {card.source_id} | {card.title} | {card.source_type} "
                f"| {_join(card.subjects)} | {freshness} |"
            )
        return "\n".join(lines) + "\n"

    def reindex(self, *, today: date | None = None) -> int:
        cards = self.load_all()
        write_text(self.index_path, self.render_index(today=today))
        return len(cards)

    # --- 세션 주입 --------------------------------------------------------

    def injection(self, *, today: date | None = None) -> str:
        """01·02 프롬프트에 넣을 블록. 카드가 없으면 빈 문자열."""
        today = today or date.today()
        cards = self.load_all()
        if not cards:
            return ""

        lines = self.render_index(today=today).splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("|"))
        table = "\n".join(lines[start:])
        return (
            "# 이미 조사한 소스\n\n"
            "아래는 이미 조사가 끝난 소스다. **재검색·재fetch를 금지한다.**\n"
            f"내용이 필요하면 `{self.sources_dir.as_posix()}/{{source_id}}.md`를 "
            "Read 도구로 읽어 참조한다.\n"
            "표에 없는 소스만 새로 검색한다. "
            "`⚠ 재확인 필요`가 붙은 소스만 예외로 재fetch를 허용한다.\n\n"
            f"{table}\n"
        )
