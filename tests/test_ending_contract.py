"""`ending.json` 계약 (specs/schema/ending.schema.json, ADR-0055).

이 계약이 막는 것은 **화면에 나가면 안 되는 것이 나가는 일**이다. 그래서 검사가
스키마 위반보다 라이선스·표시 의무 쪽으로 기운다.

값(장수·초·라이선스 목록)은 이 파일에도 코드에도 없다 — `meta`에서 로드한다
(ADR-0034 §3). 그래서 여기서는 **값이 아니라 관계**를 검사한다.
"""

import pytest

from shorts_factory.schemas import ending as ending_schema
from shorts_factory.schemas import refs as refs_schema
from shorts_factory.schemas import vocab


def photo(index=1, license_name="public-domain", **overrides):
    entry = {
        "index": index,
        "file": f"ending/{index}.mp4",
        "seconds": ending_schema.photo_seconds(),
        "source": f"refs/1/0{index}.jpg",
        "source_url": f"https://example.org/{index}.jpg",
        "license": license_name,
    }
    entry.update(overrides)
    return entry


def document(*photos, **kwargs):
    return ending_schema.build_document("20260822-x", list(photos), **kwargs)


# --- 값의 출처 -----------------------------------------------------------------


def test_the_schema_is_loaded_not_declared():
    """코드가 상수를 선언하지 않는다 (ADR-0034). meta를 고치면 코드가 따라온다."""
    assert ending_schema.ENDING_SCHEMA is vocab.ENDING_SCHEMA_DOC
    assert ending_schema.max_photos() == ending_schema.ENDING_SCHEMA["meta"]["max_photos"]


def test_the_licence_vocabulary_has_one_source():
    """게시 목록은 refs.json의 라이선스 enum 안에 있어야 한다 — 없는 값을 허용하면
    어느 사진도 그 값을 가질 수 없어 조용히 죽은 항목이 된다."""
    known = set(refs_schema.REFS_SCHEMA["$defs"]["license"]["enum"])

    assert set(ending_schema.publishable_licenses()) <= known
    assert set(ending_schema.credit_required_licenses()) <= set(
        ending_schema.publishable_licenses()
    )


def test_publish_and_attach_are_separate_axes():
    """같은 값이어도 목록은 둘이다 — 한쪽이 넓어져도 다른 쪽이 따라가면 안 된다."""
    publish = ending_schema.ENDING_SCHEMA["meta"]["publishable_licenses"]
    attach = refs_schema.REFS_SCHEMA["meta"]["attachable_licenses"]

    assert publish is not attach


# --- 게시 가능성 ---------------------------------------------------------------


@pytest.mark.parametrize("license_name", ["copyrighted", "unknown"])
def test_unpublishable_licence_is_a_contract_violation(license_name):
    errors = ending_schema.validate_ending(document(photo(license_name=license_name)))

    assert any("게시 가능" in e for e in errors)


@pytest.mark.parametrize("license_name", ending_schema.credit_required_licenses())
def test_attribution_without_a_credit_is_a_contract_violation(license_name):
    errors = ending_schema.validate_ending(document(photo(license_name=license_name)))

    assert any("표시가 의무" in e for e in errors)


@pytest.mark.parametrize("license_name", ending_schema.credit_required_licenses())
def test_attribution_with_a_credit_passes(license_name):
    valid = document(photo(license_name=license_name, credit="촬영자"))

    assert ending_schema.validate_ending(valid) == []


# --- 표시 순서 -----------------------------------------------------------------


def test_index_must_match_the_position():
    """`[9]`가 배열 순서대로 붙이므로 index가 어긋나면 화면 순서와 기록이 갈린다."""
    errors = ending_schema.validate_ending(document(photo(1), photo(3)))

    assert any("표시 순서와 어긋난다" in e for e in errors)


def test_more_photos_than_the_cap_is_a_violation():
    over = [photo(i) for i in range(1, ending_schema.max_photos() + 2)]

    errors = ending_schema.validate_ending(document(*over))

    assert any("상한" in e for e in errors)


def test_the_cap_itself_passes():
    ok = [photo(i) for i in range(1, ending_schema.max_photos() + 1)]

    assert ending_schema.validate_ending(document(*ok)) == []


# --- 없는 것이 정상이다 ---------------------------------------------------------


def test_an_empty_ending_is_valid():
    """쓸 사진이 없는 편도 계약을 지킨 것이다 (D-3). 다만 `[8]`은 이 문서를 쓰지 않는다."""
    assert ending_schema.validate_ending(document()) == []


def test_zero_seconds_is_rejected():
    errors = ending_schema.validate_ending(document(photo(seconds=0)))

    assert errors


def test_unknown_fields_are_rejected():
    """계약에 없는 필드가 붙으면 어느 단계가 그것을 읽어야 하는지가 흐려진다."""
    errors = ending_schema.validate_ending(document(photo(mood="좋음")))

    assert errors


# --- 기각 기록 -----------------------------------------------------------------


def test_rejections_record_who_dropped_it():
    """대조 기각과 판정 기각은 되돌릴 조건에서 다르게 읽힌다 (ADR-0055)."""
    valid = document(
        rejected=[{
            "source": "refs/1/02.jpg",
            "by": ending_schema.BY_REVIEW,
            "reason": "인물이 식별된다",
        }]
    )

    assert ending_schema.validate_ending(valid) == []


def test_an_unknown_rejector_is_a_violation():
    errors = ending_schema.validate_ending(
        document(rejected=[{"source": "a.jpg", "by": "누군가", "reason": "x"}])
    )

    assert errors


# --- 표기가 갈리지 않는다 -------------------------------------------------------


def test_every_publishable_licence_has_a_screen_label():
    """`kenburns.py`가 역방향 표를 어휘와 대조하는 것과 같은 장치 (ADR-0034 §3).

    표기가 빠지면 화면에 `cc-by-nd` 같은 슬러그가 그대로 굽힌다. 로드 시점에 잡는다 —
    렌더 도중이 아니라.
    """
    from shorts_factory.video.ending import LICENSE_LABELS

    assert set(ending_schema.publishable_licenses()) <= set(LICENSE_LABELS)
