import pytest

from shorts_factory.slug import MAX_SLUG_LEN, romanize, slugify


@pytest.mark.parametrize(
    "korean,expected",
    [
        ("각자성석", "gakjaseongseok"),
        ("한양도성", "hanyangdoseong"),
        ("경복궁", "gyeongbokgung"),
        ("흥인지문", "heunginjimun"),
        # 종성 인덱스가 밀리면 바로 깨지는 자리들
        ("성", "seong"),   # ㅇ
        ("값", "gap"),     # ㅄ
        ("있", "it"),      # ㅆ
        ("옷", "ot"),      # ㅅ
        ("맛", "mat"),     # ㅅ
        ("좋", "jot"),     # ㅎ
    ],
)
def test_romanize_syllables(korean, expected):
    assert romanize(korean) == expected


def test_no_assimilation_is_applied():
    """음운 변화를 적용하지 않는 표기다 (결정성 우선). 'dwissan'이 아니라 'dwitsan'."""
    assert romanize("뒷산") == "dwitsan"


def test_slugify_spec_topic():
    assert slugify("한양도성 각자성석") == "hanyangdoseong-gakjaseongseok"


def test_slugify_is_deterministic():
    """폴더 이름이므로 같은 입력은 언제나 같은 슬러그여야 한다."""
    assert slugify("한양도성 각자성석") == slugify("한양도성 각자성석")


def test_slugify_strips_punctuation_and_case():
    assert slugify("경복궁, 뒷산 벌목 금지!") == "gyeongbokgung-dwitsan-beolmok-geumji"


def test_slugify_handles_latin():
    assert slugify("Hanyang Doseong") == "hanyang-doseong"


def test_slugify_caps_length_at_hyphen_boundary():
    slug = slugify("한양도성 각자성석 " * 5)
    assert len(slug) <= MAX_SLUG_LEN
    assert not slug.endswith("-")


@pytest.mark.parametrize("bad", ["", "   ", "!!!"])
def test_slugify_rejects_unusable(bad):
    with pytest.raises(ValueError):
        slugify(bad)
