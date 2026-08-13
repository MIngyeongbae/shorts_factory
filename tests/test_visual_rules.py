"""시각 연출 어휘 (ADR-0033: 연출은 닫힌 어휘에서 고른다).

여기서 지키는 것은 둘이다.

1. **어휘가 스스로 성립하는가** — 구도 토큰마다 샷 문구와 스케일이 있고, 오버레이마다
   배제 문구가 있고, 기본값 표가 가리키는 값이 전부 실재하는가
2. **고른 값이 이긴다** — 씬 계약의 `framing`이 기본값을 이기고, 비었을 때만 떨어지는가

프롬프트 문구의 미학은 시험 대상이 아니다.
"""

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.scenes import BEATS, CAMERAS, SUBJECT_SCALES, TRANSITIONS
from shorts_factory.schemas.visual_rules import (
    FRAMINGS,
    GLOBAL_NEGATIVES,
    GLOBAL_OVERLAYS,
    OVERLAYS,
    build_negative,
    build_prompt,
    resolve_framing,
)


def scene(**overrides):
    base = {"beat": "hook_fact", "subject_scale": "wide"}
    base.update(overrides)
    return base


# --- 어휘가 스스로 성립하는가 ------------------------------------------------


@pytest.mark.parametrize("token", sorted(FRAMINGS))
def test_every_framing_token_has_a_shot_and_a_real_scale(token):
    """구도 토큰은 프롬프트에 들어갈 문구와 어울리는 스케일을 함께 갖는다."""
    framing = FRAMINGS[token]
    assert framing.shot.strip()
    assert framing.scale in SUBJECT_SCALES


def test_framing_vocabulary_matches_the_enum():
    """`$defs`의 목록과 `meta`의 항목이 갈리면 고를 수 없는 값이 생긴다."""
    assert set(FRAMINGS) == set(vocab.values("framing"))


def test_every_scale_has_at_least_one_framing():
    """한 스케일이라도 비면 그 씬이 고를 값이 없다."""
    covered = {framing.scale for framing in FRAMINGS.values()}
    assert covered == set(SUBJECT_SCALES)


# --- 기본값 표 (폴백이지 지시가 아니다, ADR-0033) ----------------------------


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_covers_every_scale(beat):
    """기본값은 (beat × subject_scale) 전 칸이 채워져 있어야 한다.

    한 칸이라도 비면 그 조합에서 씬이 `framing`을 비웠을 때 떨어질 곳이 없다.
    """
    defaults = vocab.beat_default(beat)["framing"]
    assert set(defaults) == set(SUBJECT_SCALES)
    for token in defaults.values():
        assert token in FRAMINGS


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_transition_is_in_the_vocabulary(beat):
    assert vocab.default_transition(beat) in TRANSITIONS


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_cameras_and_overlays_are_real(beat):
    """기본값 표가 어휘 밖의 값을 가리키면 되돌릴 때 그대로 깨진다."""
    defaults = vocab.beat_default(beat)
    assert set(defaults["camera"]) <= set(CAMERAS)
    assert defaults["camera"]
    for name in defaults["overlay"]:
        assert name in OVERLAYS


def test_unknown_beat_still_gets_a_default():
    """어휘가 늘었는데 기본값 표가 안 따라온 것으로 파이프라인을 세우지 않는다 (D-5)."""
    assert vocab.default_framing("아직_없는_비트", "wide") in FRAMINGS
    assert vocab.default_transition("아직_없는_비트") in TRANSITIONS


# --- 고른 값이 이긴다 (ADR-0033 §3) ------------------------------------------


def test_scene_framing_wins_over_the_default():
    assert resolve_framing(scene(framing="cross_section")) == ("cross_section", "scene")


def test_empty_framing_falls_back_to_the_beat_default():
    token, source = resolve_framing(scene())
    assert (token, source) == (vocab.default_framing("hook_fact", "wide"), "beat_default")


def test_framing_outside_the_vocabulary_falls_back_instead_of_passing_through():
    """어휘 밖의 값을 그대로 프롬프트에 실으면 검증한 적 없는 문자열이 나간다."""
    token, source = resolve_framing(scene(framing="cinematic_dolly_zoom"))
    assert source == "beat_default"
    assert token in FRAMINGS


def test_the_default_ignores_scale_mismatch_of_the_chosen_token():
    """`close` 씬이 도해 구도를 고를 수 있다 — scale은 표시이지 강제가 아니다."""
    token, source = resolve_framing(scene(subject_scale="close", framing="cross_section"))
    assert (token, source) == ("cross_section", "scene")


# --- 오버레이 (ADR-0002 2계층, ADR-0019 레이어 A 폐기) -----------------------


def test_layer_a_is_not_used_at_all():
    """ADR-0019 — 빨간 어노테이션을 전부 버렸다. 남는 것은 후처리 합성뿐이다."""
    assert {overlay.layer for overlay in OVERLAYS.values()} == {"B"}


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_text_overlays_live_in_layer_b(name):
    """읽어야 하는 값이 들어가는 오버레이는 전부 후처리 합성이다."""
    overlay = OVERLAYS[name]
    if overlay.needs_value:
        assert overlay.layer == "B"


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_every_overlay_can_be_excluded_from_the_base_image(name):
    """베이스는 클린 이미지다 (ADR-0005·0019). 배제 문구가 없으면 그걸 보장 못 한다."""
    assert OVERLAYS[name].negative


def test_global_overlays_are_the_all_scene_ones():
    """전 씬 공통 오버레이는 씬 항목이 아니라 style 블록에 실린다."""
    assert {item["type"] for item in GLOBAL_OVERLAYS} == {
        name for name, item in vocab.meta("overlay").items()
        if item.get("scope") == "all_scenes"
    }


# --- 프롬프트 조립 -----------------------------------------------------------


def test_prompt_forbids_rendering_the_korean_subject_as_text():
    """피사체는 한국어 그대로 넣되 글자로 그리지 말라고 못박는다 (ADR-0002)."""
    prompt = build_prompt("wide shot", "기울어진 탑에 겹쳐진 각도 눈금")
    assert "기울어진 탑에 겹쳐진 각도 눈금" in prompt
    assert "do not write these words in the image" in prompt


def test_negative_always_excludes_subtitles_and_particles():
    negative = build_negative(())
    for phrase in GLOBAL_NEGATIVES:
        assert phrase in negative


def test_negative_excludes_this_scene_overlays_too():
    negative = build_negative(("big_red_text",))
    assert OVERLAYS["big_red_text"].negative in negative
