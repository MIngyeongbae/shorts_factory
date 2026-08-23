"""시각 연출 어휘와 영상 프롬프트 골격 (ADR-0033·0056).

여기서 지키는 것은 셋이다.

1. **어휘가 스스로 성립하는가** — 구도 토큰마다 샷 문구와 스케일이 있고, 무대·카메라·
   계측 표시마다 영상 문구가 있고, 기본값 표가 가리키는 값이 전부 실재하는가
2. **고른 값이 이긴다** — 씬 계약의 `framing`·`staging`이 기본값을 이기고, 비었을 때만
   떨어지는가
3. **골격은 순서와 치환뿐이다** — 절이 스펙 03의 순서로 나오고, 영어 문구는 전부
   `vocab.json`의 값과 같은가 (코드가 문장을 들고 있지 않다 — ADR-0034)

프롬프트 문구의 미학은 시험 대상이 아니다.
"""

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.scenes import (
    ANNOTATIONS as ANNOTATION_ENUM,
    BEATS,
    CAMERAS,
    STAGINGS as STAGING_ENUM,
    SUBJECT_SCALES,
    TRANSITIONS,
)
from shorts_factory.schemas.visual_rules import (
    ANNOTATION_CLOSING,
    ANNOTATIONS,
    AUDIO_NEGATIVE,
    BASE_STYLE,
    CAMERA_PROMPTS,
    COMPOSITION,
    FRAMINGS,
    FROM_DEFAULT,
    FROM_SCENE,
    GLOBAL_NEGATIVES,
    NO_TEXT_NEGATIVES,
    SECONDS_PLACEHOLDER,
    SECTIONS,
    STAGINGS,
    build_video_prompt,
    negative_items,
    red_line,
    resolve_framing,
    resolve_staging,
    split_label_clause,
)


def scene(**overrides):
    base = {"beat": "hook_fact", "subject_scale": "wide"}
    base.update(overrides)
    return base


def prompt_of(**overrides) -> str:
    args = dict(
        subject="피사체", shot=FRAMINGS["drone_wide"].shot,
        staging="studio", camera="static",
    )
    args.update(overrides)
    return build_video_prompt(**args)[0]


def sections(prompt: str) -> list[str]:
    """프롬프트 줄 → 절 이름 목록."""
    return [line.split(":", 1)[0] for line in prompt.splitlines()]


INFO = {"labels": ["221 m"], "target": "the full height of the dam", "annotation": "dimension"}


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


def test_loader_exposes_the_new_axes():
    """ADR-0056 — staging·annotation·unit 어휘와 camera의 영상 문구를 로더가 낸다."""
    assert set(vocab.values("staging")) == set(STAGING_ENUM) == set(STAGINGS)
    assert set(vocab.values("annotation")) == set(ANNOTATION_ENUM) == set(ANNOTATIONS)
    assert vocab.values("unit")
    assert set(CAMERA_PROMPTS) == set(CAMERAS)
    for camera in CAMERAS:
        assert vocab.video_prompt(camera).strip()
    assert "motion" not in vocab.VOCAB["$defs"]
    assert "dialect" not in vocab.VOCAB["$defs"]


@pytest.mark.parametrize("token", sorted(STAGINGS))
def test_every_staging_has_a_phrase(token):
    assert STAGINGS[token] == vocab.phrase("staging", token)
    assert STAGINGS[token].strip()


@pytest.mark.parametrize("token", sorted(ANNOTATIONS))
def test_every_annotation_phrase_has_its_slots_and_a_label_clause(token):
    """`{target}`·`{label}` 자리와 ' and … {label}' 라벨 절 — 여러 라벨을 잇는 근거다."""
    phrase = ANNOTATIONS[token]
    assert "{target}" in phrase and "{label}" in phrase
    head, clause = split_label_clause(phrase)
    assert "{target}" in head and "{label}" in clause
    assert clause.startswith(" and ")


def test_unit_symbols_are_ascii():
    """라벨은 ASCII만이다 (ADR-0056 결정 3) — 단위 어휘가 먼저 그래야 한다."""
    for unit in vocab.values("unit"):
        assert unit.isascii() and unit.isprintable()


def test_phrase_outside_the_vocabulary_fails_loudly():
    with pytest.raises(ValueError):
        vocab.phrase("staging", "underwater")
    with pytest.raises(ValueError):
        vocab.video_prompt("dolly_zoom")


# --- 기본값 표 (폴백이지 지시가 아니다, ADR-0033) ----------------------------


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_covers_every_scale(beat):
    """기본값은 (beat × subject_scale) 전 칸이 채워져 있어야 한다."""
    defaults = vocab.beat_default(beat)["framing"]
    assert set(defaults) == set(SUBJECT_SCALES)
    for token in defaults.values():
        assert token in FRAMINGS


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_transition_is_in_the_vocabulary(beat):
    assert vocab.default_transition(beat) in TRANSITIONS


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_staging_is_in_the_vocabulary(beat):
    """비면 studio다 (ADR-0056 결정 4) — 기본값이 어휘 밖이면 그 약속이 깨진다."""
    assert vocab.default_staging(beat) in STAGINGS


@pytest.mark.parametrize("beat", BEATS)
def test_beat_default_cameras_are_real(beat):
    defaults = vocab.beat_default(beat)
    assert set(defaults["camera"]) <= set(CAMERAS)
    assert defaults["camera"]


def test_unknown_beat_still_gets_a_default():
    """어휘가 늘었는데 기본값 표가 안 따라온 것으로 파이프라인을 세우지 않는다 (D-5)."""
    assert vocab.default_framing("아직_없는_비트", "wide") in FRAMINGS
    assert vocab.default_transition("아직_없는_비트") in TRANSITIONS
    assert vocab.default_staging("아직_없는_비트") in STAGINGS


def test_there_is_no_motion_default_any_more():
    """전 씬이 영상 클립이다 (ADR-0056) — motion 기본값이 되살아나면 소비자 없는 값이다."""
    assert "motion" not in vocab.BEAT_DEFAULTS["fallback"]
    assert not hasattr(vocab, "default_motion")


# --- 고른 값이 이긴다 (ADR-0033 §3) ------------------------------------------


def test_scene_framing_wins_over_the_default():
    assert resolve_framing(scene(framing="cross_section")) == ("cross_section", FROM_SCENE)


def test_empty_framing_falls_back_to_the_beat_default():
    token, source = resolve_framing(scene())
    assert (token, source) == (vocab.default_framing("hook_fact", "wide"), FROM_DEFAULT)


def test_framing_outside_the_vocabulary_falls_back_instead_of_passing_through():
    """어휘 밖의 값을 그대로 프롬프트에 실으면 검증한 적 없는 문자열이 나간다."""
    token, source = resolve_framing(scene(framing="cinematic_dolly_zoom"))
    assert source == FROM_DEFAULT
    assert token in FRAMINGS


def test_the_default_ignores_scale_mismatch_of_the_chosen_token():
    """`close` 씬이 도해 구도를 고를 수 있다 — scale은 표시이지 강제가 아니다."""
    token, source = resolve_framing(scene(subject_scale="close", framing="cross_section"))
    assert (token, source) == ("cross_section", FROM_SCENE)


def test_scene_staging_wins_and_empty_staging_falls_back():
    assert resolve_staging(scene(staging="location")) == ("location", FROM_SCENE)
    assert resolve_staging(scene()) == (vocab.default_staging("hook_fact"), FROM_DEFAULT)
    assert resolve_staging(scene(staging="underwater"))[1] == FROM_DEFAULT


# --- 오버레이 (ADR-0002 2계층, ADR-0054 삭제) ---------------------------------


def test_the_overlay_vocabulary_is_gone():
    """ADR-0054 — 오버레이 합성이 삭제됐다. 어휘가 되살아나면 소비자 없는 계약이다."""
    assert "overlay" not in vocab.VOCAB.get("meta", {})


# --- 골격: 순서 (스펙 03 「프롬프트 골격」) ----------------------------------


def test_sections_come_in_the_spec_order_for_a_plain_scene():
    assert sections(prompt_of()) == [s for s in SECTIONS if s != "RED"]


def test_sections_come_in_the_spec_order_for_an_info_scene():
    assert sections(prompt_of(info=INFO)) == list(SECTIONS)


def test_red_appears_exactly_once_and_only_on_info_scenes():
    """어휘 문구가 이미 `RED:`로 시작한다 — 표제를 겹쳐 붙이지 않는다."""
    assert prompt_of().count("RED:") == 0
    assert prompt_of(info=INFO).count("RED:") == 1


# --- 골격: 문구는 전부 어휘의 것이다 (ADR-0034) -------------------------------


def test_format_carries_the_placeholder_and_the_vocab_style():
    """길이는 [7]이 채운다 — [5]는 초 수를 쓰지 않는다 (스펙 05)."""
    line = prompt_of().splitlines()[0]
    assert line.startswith("FORMAT:")
    assert SECONDS_PLACEHOLDER in line
    assert COMPOSITION in line and BASE_STYLE in line
    assert BASE_STYLE == vocab.style("base_style")


@pytest.mark.parametrize("token", sorted(STAGINGS))
def test_staging_line_is_the_vocab_phrase_verbatim(token):
    line = prompt_of(staging=token).splitlines()[1]
    assert line == f"STAGING: {vocab.phrase('staging', token)}"


@pytest.mark.parametrize("camera", CAMERAS)
def test_camera_line_is_the_vocab_video_prompt(camera):
    line = next(l for l in prompt_of(camera=camera).splitlines() if l.startswith("CAMERA:"))
    assert vocab.video_prompt(camera) in line


def test_subject_line_order_is_subject_anchor_description_appearance_then_shot():
    line = next(
        l for l in prompt_of(
            subject="댐", anchors=["Hoover Dam", "콘크리트"], description="실사 서술",
            appearances=["잿빛 승복의 승려"],
        ).splitlines()
        if l.startswith("SUBJECT:")
    )
    assert line.startswith("SUBJECT: 댐, Hoover Dam, 콘크리트, 실사 서술, 잿빛 승복의 승려.")
    assert line.endswith(FRAMINGS["drone_wide"].shot + ".")


def test_subject_is_not_translated_and_not_nailed_with_extra_prose():
    """한국어 그대로다 (ADR-0001·0014). 즉흥 제약을 코드가 더하지 않는다 (스펙 03)."""
    prompt = prompt_of(subject="홈이 파인 블록 접합면 클로즈업")
    assert "홈이 파인 블록 접합면 클로즈업" in prompt
    assert "do not write" not in prompt.lower()


@pytest.mark.parametrize("token", sorted(ANNOTATIONS))
def test_red_line_is_the_vocab_phrase_with_slots_filled(token):
    line = red_line(annotation=token, target="the diameter of the hole", labels=["4 mm"])
    expected = (
        vocab.phrase("annotation", token)
        .replace("{target}", "the diameter of the hole")
        .replace("{label}", "4 mm")
    )
    assert line == f"{expected} {ANNOTATION_CLOSING}"
    assert ANNOTATION_CLOSING == vocab.annotation_closing()


def test_multiple_labels_each_get_their_own_reads_exactly():
    """스펙 03 — 여러 라벨이면 각각 "reads exactly"로 잇는다. 라벨 문자열은 계약 그대로다."""
    line = red_line(annotation="dimension", target="the span", labels=["221 m", "3x"])
    assert line.count("reads exactly") == 2
    assert '"221 m"' in line and '"3x"' in line
    assert line.endswith(ANNOTATION_CLOSING)
    # 두 번째 라벨 절은 어휘 문구의 라벨 절 그대로다 — 코드가 지어낸 문장이 아니다.
    _head, clause = split_label_clause(vocab.phrase("annotation", "dimension"))
    assert clause.replace("{label}", "3x") in line


def test_red_line_rejects_unknown_annotation_and_empty_labels():
    with pytest.raises(ValueError):
        red_line(annotation="circle", target="t", labels=["1 m"])
    with pytest.raises(ValueError):
        red_line(annotation="arrow", target="t", labels=[])


def test_negative_line_is_built_from_vocab_negatives():
    """'No …' 문장으로 잇는다 (vocab negatives._role). 항목은 전부 어휘의 것이다."""
    line = prompt_of().splitlines()[-1]
    assert line.startswith("NEGATIVE:")
    for item in GLOBAL_NEGATIVES:
        assert f"no {item}" in line.lower()
    assert line.endswith(AUDIO_NEGATIVE)
    assert tuple(vocab.negatives("always")) == GLOBAL_NEGATIVES
    assert tuple(vocab.negatives("no_text")) == NO_TEXT_NEGATIVES
    assert vocab.negatives("audio") == AUDIO_NEGATIVE


def test_no_text_is_forbidden_only_when_there_is_no_info():
    """info 씬은 RED 절의 마무리 문장이 글자 금지를 대신한다 (vocab negatives._role)."""
    plain = prompt_of().splitlines()[-1]
    info = prompt_of(info=INFO).splitlines()[-1]
    for item in NO_TEXT_NEGATIVES:
        assert f"no {item}" in plain.lower()
        assert f"no {item}" not in info.lower()
    assert set(negative_items(has_info=False)) == set(GLOBAL_NEGATIVES) | set(NO_TEXT_NEGATIVES)
    assert set(negative_items(has_info=True)) == set(GLOBAL_NEGATIVES)


def test_negative_prompt_field_is_the_item_list():
    _prompt, negative = build_video_prompt(
        subject="피사체", shot="shot", staging="studio", camera="static",
    )
    assert negative == ", ".join(negative_items(has_info=False))


def test_builder_rejects_values_outside_the_vocabulary():
    with pytest.raises(ValueError):
        build_video_prompt(subject="s", shot="shot", staging="underwater", camera="static")
    with pytest.raises(ValueError):
        build_video_prompt(subject="s", shot="shot", staging="studio", camera="dolly_zoom")


def _string_literals(path) -> list[str]:
    """모듈의 문자열 리터럴 — 독스트링(설명)은 뺀다. 코드가 **값으로 드는** 문자열만 본다."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_prompt_module_holds_no_english_sentence_of_its_own():
    """골격 코드의 모든 영어 문장은 어휘 값이어야 한다 — 코드가 값으로 드는 영어 문장이 없다."""
    import re
    from pathlib import Path

    import shorts_factory.schemas.visual_rules as module
    import shorts_factory.stages.prompt as stage

    for source in (Path(module.__file__), Path(stage.__file__)):
        for literal in _string_literals(source):
            words = literal.strip().split()
            # 영어 단어 4개 이상이 이어지는 리터럴 — 문장이다. 어휘에서 온 값은 리터럴이
            # 아니라 vocab 호출이므로 여기 걸릴 수 없다.
            if len(words) >= 4 and all(re.fullmatch(r"[A-Za-z,.'-]+", w) for w in words):
                raise AssertionError(
                    f"{source.name}: 코드가 영어 문장을 들고 있다: {literal!r} (ADR-0034)"
                )
