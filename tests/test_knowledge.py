"""소스 카드 라이브러리 계약 검증 (ADR-0012).

핵심 확인 대상:
- 같은 문서를 가리키는 URL은 같은 source_id로 떨어진다 (카드 중복 = 기능 실패)
- 카드 쓰기 → 읽기 왕복이 무손실이다 (파이썬만 쓰고 읽는다)
- 같은 사실을 다른 토픽이 뽑으면 새 줄이 아니라 교차확인이 된다
- 계약 파싱 실패가 예외가 아니라 경고다
"""

from datetime import date

import pytest

from shorts_factory.knowledge import (
    Fact,
    KnowledgeError,
    KnowledgeStore,
    SourceCard,
    extract_contract,
    make_source_id,
    normalize_url,
    parse_card,
    render_card,
)

TODAY = date(2026, 8, 7)
URL = "https://seoulcitywall.seoul.go.kr/content/8.do"


def _contract(url: str = URL, **overrides) -> dict:
    entry = {
        "url": url,
        "title": "서울 한양도성 — 도성의 역사",
        "type": "reference",
        "subjects": ["한양도성"],
        "excerpts": ["숙종 이후에는 감독관·책임기술자·날짜 등을 명기하였다."],
        "facts": [{"claim": "실명 각인은 숙종 대 이후에 나타난다.", "confidence": "high"}],
    }
    entry.update(overrides)
    return {"reused": [], "new": [entry]}


# --- source_id ------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "http://seoulcitywall.seoul.go.kr/content/8.do",
        "https://www.seoulcitywall.seoul.go.kr/content/8.do",
        "https://seoulcitywall.seoul.go.kr/content/8.do/",
        "https://seoulcitywall.seoul.go.kr/content/8.do#top",
        "https://seoulcitywall.seoul.go.kr/content/8.do?utm_source=x",
        "  https://seoulcitywall.seoul.go.kr/content/8.do  ",
        "seoulcitywall.seoul.go.kr/content/8.do",
    ],
)
def test_same_document_gets_same_id(variant):
    """표기 차이로 카드가 두 장이 되면 재조사 방지라는 목적 자체가 무너진다."""
    assert make_source_id(variant) == make_source_id(URL)


def test_different_documents_get_different_ids():
    other = "https://seoulcitywall.seoul.go.kr/content/9.do"
    assert make_source_id(other) != make_source_id(URL)


def test_id_carries_readable_host_prefix():
    assert make_source_id(URL).startswith("seoulcitywall-seoul-go-kr-")


def test_meaningful_query_is_kept():
    a = "https://sillok.history.go.kr/search?id=kda_400020"
    b = "https://sillok.history.go.kr/search?id=kda_400021"
    assert make_source_id(a) != make_source_id(b)


def test_empty_url_raises():
    with pytest.raises(KnowledgeError):
        normalize_url("   ")


# --- 카드 왕복 ------------------------------------------------------------


def test_card_roundtrip_is_lossless():
    card = SourceCard(
        source_id=make_source_id(URL),
        title="서울 한양도성 — 도성의 역사",
        url=URL,
        source_type="reference",
        subjects=["한양도성", "낙산"],
        topics=["hanyangdoseong-gakjaseongseok"],
        first_researched="2026-08-07",
        last_verified="2026-08-07",
        excerpts=["숙종 이후에는 감독관·책임기술자·날짜 등을 명기하였다."],
        facts=[Fact("실명 각인은 숙종 대 이후다.", "high", ["hanyangdoseong-gakjaseongseok"])],
    )
    assert parse_card(render_card(card)) == card


def test_title_with_colon_survives_roundtrip():
    """frontmatter를 첫 콜론에서만 자르는지 확인한다."""
    card = SourceCard(source_id="x-00000000", title="설왕설래: 각자성석", url=URL)
    assert parse_card(render_card(card)).title == "설왕설래: 각자성석"


def test_broken_card_raises():
    with pytest.raises(KnowledgeError):
        parse_card("frontmatter가 없는 본문")


# --- 신선도 ---------------------------------------------------------------


def test_primary_never_expires():
    card = SourceCard("x", "t", URL, "primary", last_verified="2000-01-01")
    assert card.freshness == "permanent"
    assert not card.is_stale(TODAY)


def test_status_expires_after_90_days():
    fresh = SourceCard("x", "t", URL, "status", last_verified="2026-06-01")
    stale = SourceCard("y", "t", URL, "status", last_verified="2026-01-01")
    assert not fresh.is_stale(TODAY)
    assert stale.is_stale(TODAY)


def test_reference_expires_after_365_days():
    card = SourceCard("x", "t", URL, "reference", last_verified="2025-01-01")
    assert card.freshness == "365d"
    assert card.is_stale(TODAY)


# --- 계약 파싱 ------------------------------------------------------------


def test_extract_contract_reads_fenced_json():
    text = '# 조사\n\n본문\n\n## 참조 소스\n\n```json\n{"reused": ["a"], "new": []}\n```\n'
    assert extract_contract(text) == {"reused": ["a"], "new": []}


def test_extract_contract_returns_none_without_section():
    assert extract_contract("# 조사\n\n본문만 있다") is None


def test_extract_contract_returns_none_on_broken_json():
    assert extract_contract("## 참조 소스\n\n```json\n{깨진\n```") is None


# --- 저장소 ---------------------------------------------------------------


def test_apply_creates_card_and_stamps_topic(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    created, updated, warnings = store.apply(_contract(), slug="topic-a", today=TODAY)

    assert (created, updated, warnings) == (1, 0, [])
    card = store.load(make_source_id(URL))
    assert card.topics == ["topic-a"]
    assert card.first_researched == "2026-08-07"
    assert card.facts[0].cross_checked_by == ["topic-a"]


def test_same_fact_from_another_topic_becomes_cross_check(tmp_path):
    """다른 토픽이 같은 소스에서 같은 사실을 뽑았다 = 교차 확인이다."""
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(), slug="topic-a", today=TODAY)
    store.apply(_contract(), slug="topic-b", today=TODAY)

    card = store.load(make_source_id(URL))
    assert len(card.facts) == 1
    assert card.facts[0].cross_checked_by == ["topic-a", "topic-b"]
    assert card.topics == ["topic-a", "topic-b"]
    assert card.excerpts == ["숙종 이후에는 감독관·책임기술자·날짜 등을 명기하였다."]


def test_new_fact_is_appended_not_merged(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(), slug="topic-a", today=TODAY)
    store.apply(
        _contract(facts=[{"claim": "전혀 다른 사실이다.", "confidence": "medium"}]),
        slug="topic-b",
        today=TODAY,
    )
    assert len(store.load(make_source_id(URL)).facts) == 2


def test_reused_card_records_topic(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(), slug="topic-a", today=TODAY)

    source_id = make_source_id(URL)
    created, updated, warnings = store.apply(
        {"reused": [source_id], "new": []}, slug="topic-b", today=TODAY
    )
    assert (created, updated, warnings) == (0, 1, [])
    assert store.load(source_id).topics == ["topic-a", "topic-b"]


def test_reused_unknown_card_warns_without_raising(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    _, _, warnings = store.apply(
        {"reused": ["없는-카드-00000000"], "new": []}, slug="topic-a", today=TODAY
    )
    assert any("없는-카드-00000000" in w for w in warnings)


def test_entry_without_url_warns_and_is_skipped(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    created, _, warnings = store.apply(
        {"new": [{"title": "URL 없는 실록 인용"}]}, slug="topic-a", today=TODAY
    )
    assert created == 0
    assert any("url" in w for w in warnings)


def test_unknown_type_falls_back_with_warning(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    _, _, warnings = store.apply(_contract(type="논문"), slug="topic-a", today=TODAY)
    assert store.load(make_source_id(URL)).source_type == "reference"
    assert any("type" in w for w in warnings)


# --- 인덱스 / 주입 --------------------------------------------------------


def test_injection_is_empty_when_library_is_empty(tmp_path):
    """카드가 0장이면 첫 실행은 현행 파이프라인과 완전히 동일하게 돈다."""
    assert KnowledgeStore(tmp_path / "knowledge").injection(today=TODAY) == ""


def test_injection_forbids_refetch_and_lists_cards(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(), slug="topic-a", today=TODAY)

    text = store.injection(today=TODAY)
    assert "재검색·재fetch를 금지한다" in text
    assert make_source_id(URL) in text
    assert store.sources_dir.as_posix() in text


def test_stale_card_is_marked_in_index(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(type="status"), slug="topic-a", today=date(2026, 1, 1))
    store.reindex(today=TODAY)

    index = store.index_path.read_text(encoding="utf-8")
    assert "⚠ 재확인 필요" in index
    assert "재확인 필요 1건" in index


def test_reindex_is_deterministic(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.apply(_contract(), slug="topic-a", today=TODAY)
    store.apply(_contract("https://sillok.history.go.kr/id/kda_400020", type="primary"),
                slug="topic-a", today=TODAY)

    first = store.render_index(today=TODAY)
    assert store.render_index(today=TODAY) == first
    assert "총 2건" in first
    # 카드 순서는 파일명 정렬이라 실행마다 흔들리지 않는다
    assert first.index("seoulcitywall") < first.index("sillok-history")


def test_index_warns_against_hand_editing(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge")
    store.reindex(today=TODAY)
    assert "직접 편집하지 마라" in store.index_path.read_text(encoding="utf-8")


# --- 토픽 폴더 배치 (ADR-0026) ------------------------------------------------


def _placed(store, source_id: str):
    """카드가 실제로 놓인 경로를 sources/ 기준 상대경로로 돌려준다."""
    path = store.find_card(source_id)
    return None if path is None else path.relative_to(store.sources_dir).as_posix()


def test_new_card_lands_in_its_first_topic_folder(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.save(SourceCard("x-0001", "t", URL, topics=["pisa", "hubeodaem"]))
    assert _placed(store, "x-0001") == "pisa/x-0001.md"


def test_a_card_does_not_move_when_another_topic_cites_it(tmp_path):
    """ADR-0026의 핵심. 폴더는 표시 규약이고 소속의 진실은 topics:다.

    인용 토픽이 늘 때마다 파일을 옮기면 폴더가 계약으로 승격되고 git 이력이 흔들린다.
    """
    store = KnowledgeStore(tmp_path)
    store.save(SourceCard("x-0002", "t", URL, topics=["pisa"]))

    card = store.load("x-0002")
    card.topics.append("hubeodaem")
    store.save(card)

    assert _placed(store, "x-0002") == "pisa/x-0002.md"       # 그대로다
    assert store.load("x-0002").topics == ["pisa", "hubeodaem"]  # 소속만 늘었다


def test_a_flat_card_is_still_found_and_stays_flat(tmp_path):
    """마이그레이션 안전성 — 평면 배치 카드도 재귀 탐색으로 읽히고, 옮겨지지 않는다."""
    store = KnowledgeStore(tmp_path)
    flat = store.sources_dir / "x-0003.md"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text(render_card(SourceCard("x-0003", "t", URL, topics=["pisa"])),
                    encoding="utf-8")

    assert store.load("x-0003") is not None
    store.save(store.load("x-0003"))
    assert _placed(store, "x-0003") == "x-0003.md"


def test_a_topicless_card_is_not_lost(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.save(SourceCard("x-0004", "t", URL))
    assert _placed(store, "x-0004") == "_unfiled/x-0004.md"


def test_index_lists_a_shared_card_under_every_topic_it_belongs_to(tmp_path):
    """파일은 한 곳에만 있지만 표의 줄은 그렇지 않다 — 어느 토픽에서 찾아도 만나야 한다."""
    store = KnowledgeStore(tmp_path)
    store.save(SourceCard("shared-01", "걸치는 소스", URL, topics=["pisa", "hubeodaem"]))
    store.save(SourceCard("only-01", "피사 전용", URL, topics=["pisa"]))

    index = store.render_index(today=TODAY)
    pisa, hubeodaem = index.split("## hubeodaem")
    assert "shared-01" in pisa and "only-01" in pisa
    assert "shared-01" in hubeodaem and "only-01" not in hubeodaem


def test_index_sections_are_ordered_by_size_then_slug(tmp_path):
    """재생성할 때마다 순서가 흔들리면 diff가 의미를 잃는다."""
    store = KnowledgeStore(tmp_path)
    for i in range(3):
        store.save(SourceCard(f"big-{i}", "t", URL, topics=["zeta"]))
    store.save(SourceCard("mid-0", "t", URL, topics=["alpha"]))
    store.save(SourceCard("mid-1", "t", URL, topics=["beta"]))

    headings = [ln for ln in store.render_index(today=TODAY).splitlines()
                if ln.startswith("## ")]
    assert headings == ["## zeta (3)", "## alpha (1)", "## beta (1)"]
