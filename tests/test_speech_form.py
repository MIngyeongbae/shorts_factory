"""발화형 계약 (ADR-0063, specs/04-audio-rules.md, specs/schema/speech-rules.json).

`[3]`이 TTS에 보내는 텍스트의 규칙이다. 확인 대상:
- 세 언어의 숫자·단위가 그 언어가 읽는 대로 펴진다 (석빙고 실편 + 15편 실측의 갈래별 대표)
- **아는 것만 편다** — 사전에 없는 단위·문맥이 갈리는 조수사·범위 표기는 원문 + 경고
- 줄 수와 문장 끝 부호가 보존된다 (씬 경계의 전제 — ADR-0013)
- 사전 항목을 빼면 그 토큰이 엔진 기본 동작으로 돌아간다 (되돌릴 조건 2의 성질)
"""

import pytest

from shorts_factory.schemas import speech_rules
from shorts_factory.tts.speech import spoken_line, spoken_lines

# --- 세 언어의 읽기 ----------------------------------------------------------

#: 석빙고 실편(2026-08-24)의 숫자 토큰 다섯 개 — 사람이 어색하다고 한 그것들이다.
SEOKBINGGO = [
    ("ko", "바닥 배수로는 5도 기울여", "바닥 배수로는 오도 기울여"),
    ("ko", "녹는 양이 0.4%뿐이에요.", "녹는 양이 영 점 사퍼센트뿐이에요."),
    ("ko", "짚이 있어도 18%가 녹아요.", "짚이 있어도 십팔퍼센트가 녹아요."),
    ("ko", "한겨울 12cm 넘게 언", "한겨울 십이센티미터 넘게 언"),
    ("ko", "300년 전에 이미", "삼백년 전에 이미"),
    ("ja", "床の排水路は5度傾け、", "床の排水路はごど傾け、"),
    ("ja", "わずか0.4%。", "わずかれいてんよんパーセント。"),
    ("ja", "18%溶けます。", "じゅうはちパーセント溶けます。"),
    ("ja", "12cm以上凍った", "じゅうにセンチメートル以上凍った"),
    ("ja", "300年前に", "さんびゃくねん前に"),
    ("en", "over 12 cm thick", "over 12 centimeters thick"),
    ("en", "only 0.4% melts.", "only 0.4 percent melts."),
    ("en", "even with straw, 18% melts.", "even with straw, 18 percent melts."),
]

#: 15편 실측(topics/*/script.md)에서 뽑은 갈래별 대표. 갈래 비율은 ADR-0063 맥락 3.
KO_SAMPLES = [
    ("1cm", "일센티미터"),                       # 사람이 든 예 그대로
    ("1592년, 임진왜란이", "천오백구십이년, 임진왜란이"),   # 연 (57%)
    ("1970년대", "천구백칠십년대"),
    ("2006년엔", "이천육년엔"),
    ("8개월", "팔개월"),
    ("6월", "유월"),                              # 음이 준다 — 육월이 아니다
    ("10월", "시월"),
    ("12월", "십이월"),
    ("2억 리터가", "이억 리터가"),                  # 수 단위 (9.3%)
    ("20여만 개를", "이십여만 개를"),
    ("6mm 구멍을", "육밀리미터 구멍을"),            # 라틴 SI (8.1%)
    ("5.74km", "오 점 칠사킬로미터"),
    ("8,000 m³였죠.", "팔천세제곱미터였죠."),        # 천 단위 구분 + 띄어 쓴 라틴
    ("0.11헤르츠.", "영 점 일일헤르츠."),           # 한글 단위어 (5.8%)
    ("9초마다", "구초마다"),                       # 시간 (3.5%)
    ("8시 55분", "여덟시 오십오분"),               # 시각의 시는 고유어, 분은 한자어
    ("27단으로 쌓아", "이십칠단으로 쌓아"),          # 조수사가 한자어를 고른다
    ("40장으로", "마흔장으로"),                    # 〃 고유어
    ("33갈래", "서른세갈래"),
    ("41개 구멍", "마흔한개 구멍"),                # 21~99는 스물한·마흔한 (관형형)
    ("20개", "스무개"),                          # 일의 자리가 0이면 스무
    ("194그루입니다.", "백구십네그루입니다."),        # 100 이상은 한자어 + 고유어 일의 자리
]

JA_SAMPLES = [
    ("1592年に", "せんごひゃくきゅうじゅうにねんに"),
    ("10000円", "いちまんえん"),                    # 万은 いちまん (한국어의 만과 다르다)
    ("3分", "さんぷん"),                            # 음편
    ("1分", "いっぷん"),
    ("3階", "さんがい"),
    ("4年", "よねん"),
    ("600メートル", "ろっぴゃくメートル"),
]


@pytest.mark.parametrize("lang, text, expected", SEOKBINGGO)
def test_seokbinggo_numbers_are_spoken_in_the_locale(lang, text, expected):
    """첫 실편에서 사람이 어색하다고 한 토큰들 (ADR-0063 맥락 1)."""
    assert spoken_line(text, lang).spoken == expected


@pytest.mark.parametrize("text, expected", KO_SAMPLES)
def test_korean_numerals_follow_the_counter(text, expected):
    """한국어 수사는 조수사가 고른다 — 표가 없으면 엔진보다 나빠진다 (ADR-0063 맥락 4)."""
    assert spoken_line(text, "ko").spoken == expected


@pytest.mark.parametrize("text, expected", JA_SAMPLES)
def test_japanese_readings_include_euphony(text, expected):
    assert spoken_line(text, "ja").spoken == expected


def test_english_spells_units_but_not_numbers():
    """영어 수사는 문맥이 고른다 — 연도를 우리가 펴면 엔진보다 나빠진다 (결과 절)."""
    assert speech_rules.locale("en")["spell_numbers"] is False
    assert spoken_line("in 1592 the tower", "en").spoken == "in 1592 the tower"
    assert spoken_line("1 cm wide", "en").spoken == "1 centimeter wide"  # 단수형
    assert spoken_line("25 kg", "en").spoken == "25 kilograms"


def test_latin_abbreviations_need_a_word_boundary():
    """`12 mi`가 `12 metersi`가 되면 안 된다 — 라틴 키는 낱말 끝에서만 맞는다."""
    result = spoken_line("12 mi away", "en")
    assert result.spoken == "12 mi away"
    assert any("mi" in w for w in result.warnings)


def test_korean_skips_the_space_only_for_latin_units():
    """`3 m`은 단위지만 `1 모형`의 모형은 단위가 아니다 (`unit_space: latin` — **입력** 매칭).

    출력은 붙인다 (`unit_space_out: ""`, ADR-0079) — 입력에서 띄어 온 `3 m`도 `삼미터`가 된다.
    두 축이 다르다: 무엇을 단위로 **볼지**와 그것을 어떻게 **읽힐지**.
    """
    assert spoken_line("지반에 3 m 박힌", "ko").spoken == "지반에 삼미터 박힌"
    assert spoken_line("5분의 1 모형", "ko").spoken == "오분의 일 모형"
    assert spoken_line("5분의 1 모형", "ko").warnings == ()


# --- 아는 것만 편다 -----------------------------------------------------------


def test_ambiguous_counters_are_left_alone_with_a_warning():
    """`27대`는 왕대(한자어)일 수도 기계(고유어)일 수도 있다 — 코드가 고르지 않는다."""
    result = spoken_line("27단이 27대 선덕여왕을", "ko")

    assert "27대" in result.spoken       # 그대로 나간다 = 엔진 기본 동작
    assert "이십칠단" in result.spoken   # 아는 것은 편다
    assert len(result.warnings) == 1
    assert "units.대" in result.warnings[0]


def test_unknown_units_are_left_alone_with_a_warning():
    result = spoken_line("273계단을 올라", "ko")

    assert result.spoken == "273계단을 올라"
    assert "사전에 없어" in result.warnings[0]


def test_ranges_are_left_alone_and_warn_once():
    """`1975~79년`을 추측해서 펴지 않는다. 양쪽 숫자가 경고를 한 줄로 낸다."""
    result = spoken_line("성벽은 1975~79년에 다시 쌓았습니다.", "ko")

    assert result.spoken == "성벽은 1975~79년에 다시 쌓았습니다."
    assert len(result.warnings) == 1
    assert "1975~79" in result.warnings[0]


def test_a_hyphen_with_spaces_is_punctuation_not_a_range():
    """`1592년 — 임진왜란`의 줄표는 범위가 아니다. 떨어져 있으면 문장부호다."""
    assert spoken_line("1592년 — 임진왜란", "ko").spoken == "천오백구십이년 — 임진왜란"


def test_dropping_a_dictionary_entry_restores_the_engine_default(monkeypatch):
    """되돌리는 단위가 코드가 아니라 사전 항목 하나다 (ADR-0063 되돌릴 조건 2)."""
    assert spoken_line("12cm", "ko").spoken == "십이센티미터"

    trimmed = {
        **speech_rules.SPEECH_RULES,
        "locales": {
            **speech_rules.SPEECH_RULES["locales"],
            "ko": {
                **speech_rules.SPEECH_RULES["locales"]["ko"],
                "units": {
                    key: value
                    for key, value in speech_rules.SPEECH_RULES["locales"]["ko"]["units"].items()
                    if key != "cm"
                },
            },
        },
    }
    monkeypatch.setattr(speech_rules, "SPEECH_RULES", trimmed)

    result = spoken_line("12cm", "ko")
    assert result.spoken == "12cm"
    assert result.warnings


def test_a_language_without_a_locale_block_is_untouched(monkeypatch):
    """발화형은 품질 개선이지 계약이 아니다 — 블록이 없으면 대본 줄이 그대로 간다."""
    assert speech_rules.locale("xx") is None
    assert spoken_line("12cm", "xx").spoken == "12cm"


# --- 줄은 나뉘지도 합쳐지지도 않는다 (ADR-0013) ---------------------------------


def test_line_count_and_order_are_preserved():
    lines = ["1989년입니다.", "숫자가 없는 줄.", "12cm 입니다."]
    result = spoken_lines(lines, "ko")

    assert len(result.lines) == len(lines)
    assert result.lines[1] == lines[1]           # 건드릴 것이 없으면 그대로다
    assert [c["scene_id"] for c in result.changes] == [1, 3]


def test_sentence_endings_survive():
    """줄 끝 문장부호가 씬 경계 추출의 전제다 (`sync._sentence_end_index`)."""
    for text in ("300년.", "0.4%?", "12cm!"):
        assert spoken_line(text, "ko").spoken.endswith(text[-1])


def test_a_broken_spoken_form_falls_back_to_the_script(monkeypatch):
    """발화형이 문장 끝 부호를 먹으면 그 줄만 원문으로 돌린다 (ADR-0063 결정 6)."""
    import shorts_factory.tts.speech as speech

    monkeypatch.setattr(
        speech, "spoken_line",
        lambda text, lang: speech.SpokenLine(text=text, spoken=text.rstrip(".")),
    )
    result = speech.spoken_lines(["열두 개입니다."], "ko")

    assert result.lines == ("열두 개입니다.",)
    assert result.changes == ()
    assert "문장 끝 부호" in result.warnings[0]


# --- 계약 파일 자체 ----------------------------------------------------------


@pytest.mark.parametrize("lang", ["ko", "ja", "en"])
def test_every_unit_entry_is_readable_or_declared_ambiguous(lang):
    """사전 항목은 읽기를 갖거나 모른다고 선언해야 한다 — 둘 다 아니면 조용히 틀린다."""
    for key, entry in speech_rules.units(lang):
        if entry.get("system") == "ambiguous":
            continue
        assert entry.get("read"), f"{lang}.units.{key}에 read가 없다"
        system = entry.get("system")
        if system:
            assert speech_rules.system(lang, system), f"{lang}에 {system} 수사 체계가 없다"


def test_units_are_matched_longest_first():
    """`mm`이 `m`에, `년대`가 `년`에 먹히면 안 된다."""
    keys = [key for key, _ in speech_rules.units("ko")]
    assert keys == sorted(keys, key=len, reverse=True)
