"""스펙 01 대본 규칙 검증 (ADR-0013 단위: 자막 줄, ADR-0033 구조 자유화).

기준 픽스처는 `scenes_golden_baegak.json` — 레퍼런스 채널 "경복궁 뒷산은 왜 500년 동안
도끼가 금지됐을까"의 원본 SRT를 그대로 옮긴 것이다. 실제로 성공한 대본이 자기 검증기를
통과하는지가 이 슬라이스의 첫 시험이다.

이 픽스처는 대본 전문을 담고 있어 저장소에 올리지 않는다. 없으면 해당 테스트는 skip된다.
생성: `python tools/srt_to_scenes.py`

**막는 것은 분량과 수미상관뿐이다** (ADR-0033 §5). 구조 검증이 사라진 자리를 이 파일이
반대 방향으로 고정한다 — 비트를 흔들어도 오류가 나지 않아야 한다.
"""

import pytest

from conftest import FIXTURES, load_fixture
from shorts_factory.schemas.script_rules import (
    LINE_CHARS_MAX,
    PRIMARY_SIGNATURE,
    core_chars,
    noun_stems,
    validate_script,
)
from shorts_factory.schemas.scenes import validate_scenes

GOLDEN = "scenes_golden_baegak.json"


@pytest.fixture
def golden() -> dict:
    """골든 픽스처 사본. 없으면 skip (저작권 있는 원본 SRT에서 로컬 생성한다)."""
    if not (FIXTURES / GOLDEN).exists():
        pytest.skip(f"{GOLDEN}이 없다 — python tools/srt_to_scenes.py 로 생성한다")
    return load_fixture(GOLDEN)


def find(scenes: dict, beat: str) -> dict:
    return next(s for s in scenes["scenes"] if s["beat"] == beat)


# --- 기준 픽스처 -------------------------------------------------------------


def test_golden_script_passes_clean(golden):
    """성공한 실제 대본이 오류·경고 없이 통과해야 한다."""
    errors, warnings = validate_script(golden)
    assert errors == []
    assert warnings == []


def test_golden_script_also_satisfies_scene_schema(golden):
    """스펙 01과 스펙 02는 같은 파일을 두 각도에서 본다."""
    errors, warnings = validate_scenes(golden)
    assert errors == []
    assert warnings == []


def test_schema_fixture_is_not_a_golden_script():
    """scenes_pass.json은 스키마 픽스처라 스펙 01 분량을 만족하지 않는다.

    두 픽스처의 역할이 다르다는 사실을 고정한다.
    """
    errors, _ = validate_script(load_fixture("scenes_pass.json"))
    assert any("자막" in e for e in errors)
    assert any("총" in e and "자" in e for e in errors)


# --- 구조는 검증하지 않는다 (ADR-0033 §5) ------------------------------------


def test_beat_order_is_not_validated(golden):
    """단 구성이 소재마다 다르므로 순서를 기계가 볼 수 없다."""
    golden["scenes"][2]["beat"] = "ending_echo"
    errors, _ = validate_script(golden)
    assert errors == []


def test_missing_turning_point_beat_is_fine(golden):
    """전환점이 없는 서사도 있다. 그 판단은 `[2b] judge` 몫이다."""
    find(golden, "turning_point")["beat"] = "solution_step"
    errors, _ = validate_script(golden)
    assert errors == []


def test_single_failed_solution_is_fine(golden):
    """실패한 대안이 한 번만 나오는 편을 막지 않는다."""
    for scene in golden["scenes"]:
        if scene["beat"] == "failed_solution":
            scene["beat"] = "failure_reason"
            break
    errors, _ = validate_script(golden)
    assert errors == []


# --- 시그니처 문구: 권장이라 경고다 (ADR-0033 §2) ----------------------------


def test_missing_signature_phrase_is_a_warning(golden):
    find(golden, "turning_point")["text"] = "그래서 생각을 바꿉니다."
    errors, warnings = validate_script(golden)
    assert all(PRIMARY_SIGNATURE not in e for e in errors)
    assert any(PRIMARY_SIGNATURE in w for w in warnings)


def test_signature_position_is_not_checked(golden):
    """44~54초는 폐기된 고정 구조에서 온 값이라 자리가 없다."""
    find(golden, "turning_point")["est_start"] = 20.0
    errors, warnings = validate_script(golden)
    assert all("44" not in e for e in errors)
    assert all("44" not in w for w in warnings)


def test_dilemma_phrase_variant_is_not_required(golden):
    """'환장할 노릇'은 권장이라 없어도 통과한다 (3편 중 1편은 아예 없다)."""
    scene = find(golden, "dilemma_peak")
    scene["text"] = scene["text"].replace(" 환장할 노릇입니다.", "")
    errors, warnings = validate_script(golden)
    assert all("환장" not in e for e in errors)
    assert all("환장" not in w for w in warnings)


# --- 분량 (막는다) -----------------------------------------------------------


def test_too_few_lines_is_rejected(golden):
    golden["scenes"] = golden["scenes"][:20]
    errors, _ = validate_script(golden)
    assert any("자막" in e and "줄" in e for e in errors)


def test_too_few_chars_is_rejected(golden):
    for scene in golden["scenes"]:
        scene["text"] = scene["text"][:5]
    errors, _ = validate_script(golden)
    assert any("총" in e for e in errors)


# --- 숫자: 편 전체 기준 경고 -------------------------------------------------


def test_script_without_numbers_is_a_warning(golden):
    """비트로 구간을 자를 수 없으므로 편 전체에서 센다."""
    for scene in golden["scenes"]:
        scene["text"] = "숫자가 하나도 없는 설명 문장입니다."
    errors, warnings = validate_script(golden)
    assert all("숫자" not in e for e in errors)
    assert any("숫자" in w for w in warnings)


# --- 수미상관 (막는다) -------------------------------------------------------


def test_missing_echo_is_rejected(golden):
    for scene in golden["scenes"]:
        if scene["beat"] in ("hook_fact", "hook_twist"):
            scene["text"] = "전혀 다른 화제의 도입부 문장을 넣습니다."
    errors, _ = validate_script(golden)
    assert any("수미상관" in e for e in errors)


def test_echo_falls_back_to_the_edges_without_position_labels(golden):
    """훅·엔딩 라벨이 하나도 없는 대본에서도 수미상관은 본다.

    비트는 이제 라벨일 뿐이라 어느 편에서든 있으리라는 보장이 없다 (ADR-0033 §4).
    """
    for scene in golden["scenes"]:
        scene["beat"] = "context"
    errors, _ = validate_script(golden)
    assert all("수미상관" not in e for e in errors)

    for scene in golden["scenes"][:3]:
        scene["text"] = "전혀 다른 화제의 도입부 문장을 넣습니다."
    errors, _ = validate_script(golden)
    assert any("수미상관" in e for e in errors)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("백악이", "백악"),      # 조사 제거
        ("경복궁에서", "경복궁"),
        ("성벽은", "성벽"),
    ],
)
def test_noun_stem_strips_josa(word, expected):
    assert expected in noun_stems(word)


def test_noun_stem_drops_verbs_and_stopwords():
    assert noun_stems("있습니다 아니었죠 그런데 지금") == set()


@pytest.mark.parametrize("word", ["파비아의", "파비아가", "베네치아는", "이탈리아의", "바다에"])
def test_noun_stem_keeps_nouns_ending_in_verb_like_syllables(word):
    """아/어/야로 끝나는 명사를 용언으로 오인하지 않는다.

    도메인 제한이 없어진 뒤(ADR-0016·0033) 외래 고유명사가 기본값이다. 조사를 뗀
    어간에 용언 검사를 다시 걸면 '파비아의'→'파비아'가 통째로 사라졌고, 피사 편
    첫 대본이 실제 수미상관(hook '파비아의' ↔ ending '파비아가')을 갖고도
    실패 판정을 받았다.
    """
    assert noun_stems(word)


def test_verb_check_still_applies_to_the_bare_word():
    """어절 원형이 _VERB_END로 끝나면 조사가 없어도 버린다.

    반대 방향의 누수는 그대로다 — '무너져'(져)나 '당긴'(긴)처럼 목록에 없는
    어미의 용언은 통과한다. 그쪽은 수미상관 판정을 느슨하게 만들 뿐 오탐을
    만들지 않아 이번 수정 범위 밖에 뒀다.
    """
    assert noun_stems("높아 기울어 커졌다") == set()


def test_echo_is_found_through_a_foreign_proper_noun():
    """오탐 회귀 고정 — 고유명사 하나로만 이어진 수미상관도 통과해야 한다."""
    hook = noun_stems("파비아의 78m 시민탑이 예고 없이 주저앉습니다.")
    ending = noun_stems("카티노는 지금도 그 자리에 있습니다. 파비아가 남긴 경고와 함께.")
    assert hook & ending == {"파비아"}


def test_core_chars_excludes_whitespace_and_punctuation():
    assert core_chars("경복궁 뒤, 백악입니다.") == "경복궁뒤백악입니다"


# --- 경고 (차단하지 않음) -----------------------------------------------------


def test_overlong_line_is_warning_not_error(golden):
    target = golden["scenes"][2]
    target["text"] = "가" * (LINE_CHARS_MAX + 5)
    errors, warnings = validate_script(golden)
    assert all(str(LINE_CHARS_MAX) not in e for e in errors)
    assert any(str(LINE_CHARS_MAX) in w for w in warnings)


def test_speed_out_of_range_is_warning_not_error(golden):
    golden["total_duration"] = 200.0
    _, warnings = validate_script(golden)
    assert any("발화 속도" in w for w in warnings)
