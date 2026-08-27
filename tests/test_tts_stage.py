"""[3. tts+sync] 단계 계약 (specs/05-pipeline.md, ADR-0004/0013/0017/0049/0052/0056).

입력은 `topics/{slug}/script.md`(ko)와 `script.{ja,en}.md`(있는 것만)의 대본 줄이다 —
한 줄 = 한 씬 (ADR-0013). 확인 대상:
- 언어당 산출물 3종이 전부 `runs/{run_id}/` 아래에 떨어진다
- `topics/` 아래 무엇도 건드리지 않는다 (ADR-0017)
- `scenes.timed.{lang}.json`은 `text`+실측 시각만 담는다 — 대본 속성은 `[3s]` 소관 (specs/05)
- 총 길이 상한 초과 시 그 언어를 멈추고, 대본을 다시 만들라고 리포트한다
- 줄 수 불일치·빈 voice_id는 **호출 전에** 막는다 (ADR-0056 결정 5·7)
"""

import json

import pytest

from conftest import PISA, load_script
from shorts_factory.schemas.timed_scenes import validate_line_timed_scenes
from shorts_factory.stages.tts import (
    MAX_TOTAL_SECONDS,
    STAGE,
    TTSStageError,
    run_tts_stage,
)
from shorts_factory.tts.base import Alignment, Narration, TTSError
from shorts_factory.schemas.script_rules import TOTAL_SECONDS, core_chars
from shorts_factory.tts.fake import (
    DEFAULT_RAW_SPEED,
    NOMINAL_FINAL_SPEED,
    FakeFFmpeg,
    FakeTTSClient,
    fake_alignment,
    fake_narration,
)
from shorts_factory.tts.speech import spoken_lines
from shorts_factory.tts.sync import narration_text

RUN_ID = "20260821-tts-fixture"


def script_lines(slug: str = PISA) -> list[str]:
    """동결 계약 픽스처의 text를 대본 줄로 재활용한다 — 엔벨로프에 드는 앞부분만.

    픽스처는 90초대 엔벨로프 시절의 25줄·570자라 페이크 속도로 60초 상한(ADR-0057)을
    넘긴다. 이 단계의 테스트는 길이 게이트가 아니라 배관을 보므로, 페이크 명목 속도로
    `total_seconds` 상한 안에 드는 만큼만 쓴다 — 값은 계약에서 읽는다 (ADR-0034)."""
    budget = TOTAL_SECONDS[1] * NOMINAL_FINAL_SPEED * 0.9
    lines: list[str] = []
    used = 0
    for scene in load_script(slug)["scenes"]:
        chars = len(core_chars(scene["text"]))
        if used + chars > budget:
            break
        lines.append(scene["text"])
        used += chars
    return lines


def install_md(
    paths, slug: str = PISA, lines: list[str] | None = None, *, lang: str = "ko"
) -> None:
    """`script[.{lang}].md` + `runs/{run_id}/topic.json` — 이 단계가 아는 전부다 (ADR-0049)."""
    lines = script_lines(slug) if lines is None else lines
    body = "\n".join(["# 픽스처 대본", "", "## 대본", "", *lines])
    name = "script.md" if lang == "ko" else f"script.{lang}.md"
    md = paths.topic_dir(slug) / name
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(body, encoding="utf-8")
    run_dir = paths.run_dir(RUN_ID)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "topic.json").write_text(
        json.dumps({"run_id": RUN_ID, "slug": slug, "topic": "픽스처 소재"},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def run(paths, slug=PISA, *, tts=None, ffmpeg=None, **kwargs):
    return run_tts_stage(
        slug,
        tts=tts or FakeTTSClient(),
        paths=paths,
        runner=ffmpeg or FakeFFmpeg(),
        **kwargs,
    )


@pytest.fixture
def pisa(paths):
    """script.md + topic.json만 놓인 격리 루트."""
    install_md(paths)
    return paths


def state_of(paths, run_id):
    data = json.loads((paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][STAGE]


# --- 통과 경로 ---------------------------------------------------------------


def test_stage_produces_the_three_contract_files(pisa):
    result = run(pisa)

    assert result.passed
    run_dir = pisa.run_dir(RUN_ID)
    assert result.narration_path == run_dir / "narration.ko.wav"
    assert result.timing_path == run_dir / "timing.ko.json"
    assert result.scenes_path == run_dir / "scenes.timed.ko.json"
    for path in (result.narration_path, result.timing_path, result.scenes_path):
        assert path.exists()
    assert result.scene_count == len(script_lines())


def test_run_id_comes_from_topic_json(pisa):
    """slug→run 해석은 `runs/*/topic.json` 하나다 (ADR-0052)."""
    result = run(pisa)
    assert result.run_id == RUN_ID


def test_script_is_a_single_call_with_every_line_joined(pisa):
    """ADR-0004: 대본 전체를 단일 호출로. 문장별 분할 호출은 톤이 끊긴다."""
    tts = FakeTTSClient()
    run(pisa, tts=tts)

    assert len(tts.calls) == 1
    assert tts.calls[0]["text"] == narration_text(spoken_lines(script_lines(), "ko").lines)
    assert tts.calls[0]["label"] == f"{STAGE}:ko"


def test_the_call_carries_the_spoken_form_not_the_script(pisa):
    """ADR-0063 — 읽는 텍스트와 보는 텍스트는 다르다.

    보내는 것은 발화형이고(`12 mm` → `십이밀리미터`), 자막이 읽는 실측 파일의 `text`는
    **원문 그대로**다. 이 둘이 같아지면 자막에 풀어 쓴 숫자가 나가거나(ADR-0060 결정 4의
    라벨 에코가 깨진다) TTS가 약어를 읽게 된다.
    """
    tts = FakeTTSClient()
    result = run(pisa, tts=tts)
    sent = tts.calls[0]["text"]
    script = narration_text(script_lines())

    assert sent != script  # 픽스처에 숫자가 있다 (1989년·8,000 m³·12 mm)
    assert "mm" not in sent and "밀리미터" in sent
    # 숫자와 단위는 **붙여** 읽힌다 (ADR-0079) — 띄우면 TTS가 그 자리에서 끊는다.
    assert "8,000" not in sent and "팔천세제곱미터" in sent

    timed = json.loads(result.scenes_path.read_text(encoding="utf-8"))
    assert [scene["text"] for scene in timed["scenes"]] == script_lines()


def test_timed_scenes_carry_text_and_measured_time_only(pisa):
    """새 실측 파일은 text+시각뿐이다 — 대본 속성은 [3s]의 scenes.json 소관 (specs/05)."""
    result = run(pisa)
    timed = json.loads(result.scenes_path.read_text(encoding="utf-8"))

    assert validate_line_timed_scenes(timed) == ([], [])
    first = timed["scenes"][0]
    assert set(first) == {"scene_id", "text", "start", "end"}
    assert timed["total_duration"] == timed["scenes"][-1]["end"]
    assert [s["text"] for s in timed["scenes"]] == script_lines()


def test_timestamps_are_scaled_by_one_over_tempo(pisa):
    """specs/05: atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정."""
    result = run(pisa, tempo=1.1)

    assert result.total_duration == pytest.approx(result.raw_duration / 1.1, abs=0.01)
    timing = json.loads(result.timing_path.read_text(encoding="utf-8"))
    assert timing["tempo"] == 1.1
    assert timing["raw_duration"] > timing["total_duration"]


def test_narration_length_matches_the_scene_timeline(pisa):
    result = run(pisa)

    assert result.audio_duration == pytest.approx(result.total_duration, abs=0.5)
    assert not any("narration.wav 길이" in w for w in result.warnings)


def test_scene_timings_are_gapless(pisa):
    """[9. assemble]이 자막(ASS)을 만들 때 보는 파일이다 (ADR-0020)."""
    result = run(pisa)
    timed = json.loads(result.scenes_path.read_text(encoding="utf-8"))

    assert [s["scene_id"] for s in timed["scenes"]] == list(
        range(1, len(script_lines()) + 1)
    )
    assert timed["scenes"][0]["start"] == 0.0
    for before, after in zip(timed["scenes"], timed["scenes"][1:]):
        assert after["start"] == before["end"]  # 빈틈 없이 이어진다


def test_timing_json_is_a_record_not_a_scene_contract(pisa):
    """ADR-0020 — 씬의 시각을 두 파일이 들고 있으면 갈라진다. cues를 담지 않는다."""
    result = run(pisa)
    timing = json.loads(result.timing_path.read_text(encoding="utf-8"))

    assert "cues" not in timing
    assert set(timing) == {
        "run_id", "topic", "lang", "engine", "tempo",
        "raw_duration", "total_duration", "audio", "warnings", "spoken",
    }
    assert timing["lang"] == "ko"


def test_timing_records_what_was_spoken(pisa):
    """ADR-0063 결정 5 — 무엇을 어떻게 읽혔는지가 남아야 오디오를 다시 듣지 않는다."""
    result = run(pisa)
    timing = json.loads(result.timing_path.read_text(encoding="utf-8"))

    lines = timing["spoken"]["lines"]
    assert lines, "픽스처에 숫자가 있는데 편 줄이 기록되지 않았다"
    for change in lines:
        assert set(change) == {"scene_id", "text", "spoken"}
        assert change["text"] != change["spoken"]
        assert script_lines()[change["scene_id"] - 1] == change["text"]


def test_state_records_the_outputs(pisa):
    result = run(pisa)
    stage = state_of(pisa, result.run_id)

    assert stage["status"] == "done"
    assert stage["scene_count"] == len(script_lines())
    assert sorted(stage["outputs"]) == [
        f"runs/{result.run_id}/narration.ko.wav",
        f"runs/{result.run_id}/scenes.timed.ko.json",
        f"runs/{result.run_id}/timing.ko.json",
    ]


# --- 읽기 전용 경계 (ADR-0017) -----------------------------------------------


def test_stage_never_writes_under_topics(pisa):
    topic_dir = pisa.topic_dir(PISA)
    before = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}

    run(pisa)

    after = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}
    assert after == before
    assert list(after) == ["script.md"]


# --- 재시작 ------------------------------------------------------------------


def test_second_run_skips_and_does_not_call_the_engine(pisa):
    run(pisa)
    tts = FakeTTSClient()
    again = run(pisa, tts=tts)

    assert again.skipped and again.passed
    assert tts.calls == [], "TTS는 편당 과금이다. 완료된 단계를 다시 사지 않는다"
    assert again.total_duration > 0


def test_force_reruns_the_engine(pisa):
    run(pisa)
    tts = FakeTTSClient()
    again = run(pisa, tts=tts, force=True)

    assert not again.skipped
    assert len(tts.calls) == 1


def test_missing_output_defeats_the_skip(pisa):
    result = run(pisa)
    result.scenes_path.unlink()

    tts = FakeTTSClient()
    again = run(pisa, tts=tts)
    assert not again.skipped
    assert len(tts.calls) == 1


# --- 길이 초과 (specs/05: 리포트하고 멈춘다) ---------------------------------


@pytest.fixture
def slow_voice():
    """대본은 그대로인데 낭독이 느려 `total_seconds` 상한을 넘기는 경우.

    `script_lines()`가 상한의 90%에 맞춘 대본이므로 25% 느리면 확실히 넘긴다."""
    return FakeTTSClient(speed=DEFAULT_RAW_SPEED * 0.75)


def test_over_length_stops_before_writing_the_scene_contract(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)

    assert result.over_length
    assert not result.passed
    assert result.total_duration > MAX_TOTAL_SECONDS
    assert not (result.run_dir / "scenes.timed.ko.json").exists()


def test_over_length_removes_a_stale_contract_from_an_earlier_run(pisa, slow_voice):
    """옛 타임스탬프가 새 오디오와 짝이 맞지 않는 채로 남으면 하류가 그대로 쓴다."""
    first = run(pisa)
    assert first.scenes_path.exists()

    again = run(pisa, tts=slow_voice, force=True)

    assert again.over_length
    assert not (again.run_dir / "scenes.timed.ko.json").exists()


def test_over_length_keeps_the_audio_it_paid_for(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)

    assert result.narration_path.exists()
    assert result.timing_path.exists()


def test_over_length_reports_that_part_one_must_shorten_the_script(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)
    stage = state_of(pisa, result.run_id)

    assert stage["status"] == "failed"
    assert "대본 축약" in stage["error"]
    assert "1부" in stage["error"]
    assert "축약" in result.summary


def test_over_length_does_not_regenerate_the_script(pisa, slow_voice):
    """2부는 1부를 다시 돌리지 않는다 (ADR-0017 단방향 경계)."""
    run(pisa, tts=slow_voice)

    topic_dir = pisa.topic_dir(PISA)
    assert [p.name for p in topic_dir.rglob("*") if p.is_file()] == ["script.md"]


# --- 실패 ---------------------------------------------------------------------


def test_missing_script_points_at_part_one(paths):
    with pytest.raises(TTSStageError, match="1부가 끝난 토픽"):
        run(paths)


def test_script_without_lines_is_refused(paths):
    install_md(paths, lines=["대체될 줄."])
    md = paths.topic_dir(PISA) / "script.md"
    md.write_text("# 제목뿐\n\n- 시드: x\n", encoding="utf-8")

    with pytest.raises(TTSStageError, match="대본 줄이 없다"):
        run(paths)


def test_missing_run_points_at_seed(paths):
    """script.md는 있는데 run이 없다 — [0. seed]가 먼저다."""
    install_md(paths)
    import shutil

    shutil.rmtree(paths.run_dir(RUN_ID))
    with pytest.raises(TTSStageError, match=r"\[0\. seed\]"):
        run(paths)


def test_alignment_that_does_not_match_the_script_fails_loudly(pisa):
    """정렬이 밀리면 영상 전체의 싱크가 깨진다. 관용적으로 맞추지 않는다.

    엔진이 `normalized_alignment`(읽은 대로 편 배열)를 돌려준 경우를 흉내 낸다 —
    우리가 보낸 발화형과 글자가 다르므로 즉시 실패해야 한다 (ADR-0063 결정 2).
    """
    def wrong(text: str) -> Narration:
        return fake_narration(text.replace("천구백팔십구", "1989"))

    with pytest.raises(TTSStageError, match="정렬이 보낸 대본과 다르다") as exc:
        run(pisa, tts=FakeTTSClient([wrong]))

    assert "normalized_alignment" in str(exc.value)
    assert state_of(pisa, RUN_ID)["status"] == "failed"
    assert not (pisa.run_dir(RUN_ID) / "scenes.timed.ko.json").exists()


def test_ffmpeg_failure_fails_the_stage(pisa):
    with pytest.raises(TTSStageError, match="FFmpeg 실패"):
        run(pisa, ffmpeg=FakeFFmpeg(returncode=1, stderr="no such filter"))

    assert state_of(pisa, RUN_ID)["status"] == "failed"
    assert (pisa.run_dir(RUN_ID) / "narration.ko.raw.wav").exists()


def test_engine_error_propagates(pisa):
    """호출 실패는 어댑터가 재시도할 몫이지 이 단계가 삼킬 것이 아니다."""
    with pytest.raises(TTSError, match="한도"):
        run(pisa, tts=FakeTTSClient([TTSError("사용 한도 초과")]))


def test_line_without_sentence_punctuation_is_reported_as_a_warning(paths):
    lines = script_lines()
    lines[0] = lines[0].rstrip(".")
    install_md(paths, lines=lines)

    result = run(paths)

    assert result.passed
    assert any("문장부호로 끝나지 않아" in w for w in result.warnings)


# --- 페이크 자체 --------------------------------------------------------------


def test_fake_default_speed_lands_on_the_nominal_rate_after_atempo():
    """페이크의 원속은 atempo 1.1을 거치면 1부의 명목 5.85자/초가 된다."""
    assert DEFAULT_RAW_SPEED * 1.1 == pytest.approx(5.85)


def test_fake_alignment_covers_the_text_exactly():
    alignment = fake_alignment("가나 다.")
    assert alignment.text == "가나 다."
    assert isinstance(alignment, Alignment)


# --- 언어 루프 (ADR-0056 결정 5·7) ---------------------------------------------
#
# ko 필수 + ja·en은 대본 파일이 있으면. 목소리는 언어별이고, 줄 수 불일치와 빈 voice_id는
# **어느 언어도 부르기 전에** 막는다 — 둘째 언어에서 멈추면 첫째 언어의 과금이 헛되다.

from shorts_factory.tts.base import TTSNotConfigured
from shorts_factory.tts.elevenlabs import ElevenLabsClient


def ja_lines() -> list[str]:
    """ko와 줄 수가 같은 가짜 번안 — 내용은 중요하지 않고 줄 1:1만 중요하다."""
    return [f"シーン{i}の文です。" for i in range(1, len(script_lines()) + 1)]


def en_lines() -> list[str]:
    return [f"Scene {i} sentence." for i in range(1, len(script_lines()) + 1)]


def factory(clients: dict):
    """언어 → 페이크 클라이언트. 호출 기록을 언어별로 가른다."""
    return lambda lang: clients[lang]


def test_each_present_language_gets_three_files(pisa):
    install_md(pisa, lines=ja_lines(), lang="ja")
    install_md(pisa, lines=en_lines(), lang="en")
    clients = {"ko": FakeTTSClient(), "ja": FakeTTSClient(), "en": FakeTTSClient()}

    result = run(pisa, tts=factory(clients))

    assert result.passed and sorted(result.languages) == ["en", "ja", "ko"]
    run_dir = pisa.run_dir(RUN_ID)
    for lang in ("ko", "ja", "en"):
        for name in (f"narration.{lang}.wav", f"timing.{lang}.json", f"scenes.timed.{lang}.json"):
            assert (run_dir / name).exists(), name
        assert len(clients[lang].calls) == 1
        assert clients[lang].calls[0]["label"] == f"{STAGE}:{lang}"
    ja = json.loads((run_dir / "scenes.timed.ja.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in ja["scenes"]] == ja_lines()
    assert len(ja["scenes"]) == result.scene_count


def test_missing_translation_means_only_korean(pisa):
    """D-3 — ja 대본이 없으면 ja의 쇼츠가 없을 뿐이다. 경고도 없다."""
    calls = {"ko": FakeTTSClient(), "ja": FakeTTSClient(), "en": FakeTTSClient()}
    result = run(pisa, tts=factory(calls))

    assert list(result.languages) == ["ko"]
    assert calls["ja"].calls == [] and calls["en"].calls == []
    assert not any("ja" in w for w in result.warnings)


def test_empty_japanese_voice_id_stops_before_any_call(pisa, monkeypatch):
    """스펙 05 [3] — 대본 파일이 있는데 id가 비어 있으면 진입 전에 멈춘다. ko도 부르지 않는다."""
    install_md(pisa, lines=ja_lines(), lang="ja")
    monkeypatch.setenv("ELEVEN_VOICE_ID", "voice-ko")
    monkeypatch.delenv("ELEVEN_VOICE_ID_JA", raising=False)
    ko = FakeTTSClient()

    def boom(*_a, **_k):
        raise AssertionError("transport가 불리면 안 된다 — 호출 전에 막혀야 한다")

    ja = ElevenLabsClient(lang="ja", api_key="sk_test", transport=boom)

    with pytest.raises(TTSNotConfigured, match="ELEVEN_VOICE_ID_JA"):
        run(pisa, tts=factory({"ko": ko, "ja": ja}))

    assert ko.calls == [], "ja 설정 오류로 ko를 먼저 사 버리면 안 된다"
    assert not (pisa.run_dir(RUN_ID) / "narration.ko.wav").exists()


def test_line_count_mismatch_fails_before_any_call(pisa):
    """[2l]의 줄 1:1 정렬이 깨졌다 — 클립 풀을 공유할 수 없으므로 호출 전에 멈춘다."""
    install_md(pisa, lines=ja_lines()[:-1], lang="ja")
    clients = {"ko": FakeTTSClient(), "ja": FakeTTSClient()}

    with pytest.raises(TTSStageError, match="줄 수"):
        run(pisa, tts=factory(clients))

    assert clients["ko"].calls == [] and clients["ja"].calls == []


def test_per_language_tempo(pisa):
    install_md(pisa, lines=ja_lines(), lang="ja")
    result = run(pisa, tts=factory({"ko": FakeTTSClient(), "ja": FakeTTSClient()}),
                 tempo={"ko": 1.1, "ja": 1.2})

    assert result.languages["ko"].tempo == 1.1
    assert result.languages["ja"].tempo == 1.2
    ja_timing = json.loads((pisa.run_dir(RUN_ID) / "timing.ja.json").read_text(encoding="utf-8"))
    assert ja_timing["tempo"] == 1.2 and ja_timing["lang"] == "ja"


def test_over_length_in_one_language_stops_only_that_language(pisa):
    """ja가 넘치면 ja의 실측 파일만 빠지고 ko·en은 돈다 — 언어끼리는 독립이다."""
    install_md(pisa, lines=ja_lines(), lang="ja")
    install_md(pisa, lines=en_lines(), lang="en")
    clients = {"ko": FakeTTSClient(), "ja": FakeTTSClient(speed=1.0), "en": FakeTTSClient()}

    result = run(pisa, tts=factory(clients))

    assert result.over_length and result.over_length_languages == ["ja"]
    run_dir = pisa.run_dir(RUN_ID)
    assert (run_dir / "scenes.timed.ko.json").exists()
    assert not (run_dir / "scenes.timed.ja.json").exists()
    assert (run_dir / "narration.ja.wav").exists(), "산 오디오는 남긴다"
    assert (run_dir / "scenes.timed.en.json").exists()
    assert "ja" in result.summary and "축약" in result.summary
    stage = state_of(pisa, RUN_ID)
    assert stage["status"] == "failed"
    assert stage["languages"]["ja"]["status"] == "over_length"
    assert stage["languages"]["en"]["status"] == "done"


def test_korean_over_length_does_not_buy_the_other_languages(pisa):
    """ko를 줄이면 번안도 다시 되므로 ja·en을 사지 않는다."""
    install_md(pisa, lines=ja_lines(), lang="ja")
    clients = {"ko": FakeTTSClient(speed=DEFAULT_RAW_SPEED * 0.75), "ja": FakeTTSClient()}

    result = run(pisa, tts=factory(clients))

    assert result.over_length_languages == ["ko"]
    assert clients["ja"].calls == []
    assert "ja" not in result.languages


def test_second_run_only_buys_the_new_language(pisa):
    run(pisa)
    install_md(pisa, lines=ja_lines(), lang="ja")
    clients = {"ko": FakeTTSClient(), "ja": FakeTTSClient()}

    again = run(pisa, tts=factory(clients))

    assert again.languages["ko"].skipped and not again.languages["ja"].skipped
    assert clients["ko"].calls == [] and len(clients["ja"].calls) == 1
    assert state_of(pisa, RUN_ID)["status"] == "done"


def test_lang_option_limits_the_loop_but_keeps_korean(pisa):
    install_md(pisa, lines=ja_lines(), lang="ja")
    install_md(pisa, lines=en_lines(), lang="en")
    clients = {"ko": FakeTTSClient(), "ja": FakeTTSClient(), "en": FakeTTSClient()}

    result = run(pisa, tts=factory(clients), langs=["ja"])

    assert sorted(result.languages) == ["ja", "ko"]
    assert clients["en"].calls == []


def test_unknown_language_is_refused(pisa):
    with pytest.raises(TTSStageError, match="모르는 언어"):
        run(pisa, langs=["fr"])


def test_voice_env_is_per_language(monkeypatch):
    monkeypatch.setenv("ELEVEN_VOICE_ID_EN", "voice-en")
    assert ElevenLabsClient(lang="en", api_key="k").voice_id == "voice-en"
    monkeypatch.delenv("ELEVEN_VOICE_ID_EN")
    with pytest.raises(TTSNotConfigured, match="ELEVEN_VOICE_ID_EN"):
        ElevenLabsClient(lang="en", api_key="k").check_configured()


def test_cli_lang_option_is_parsed():
    from shorts_factory.cli import parse_args

    assert parse_args(["tts", "--slug", "x", "--lang", "ko,ja"]).lang == "ko,ja"
    assert parse_args(["tts", "--slug", "x"]).lang is None

# --- 제목 훅 (ADR-0065) -------------------------------------------------------


def test_title_travels_to_the_timed_contract(pisa):
    """`script.md`의 `# 제목`이 `scenes.timed.{lang}.json`으로 간다."""
    run(pisa)

    data = json.loads(
        (pisa.run_dir(RUN_ID) / "scenes.timed.ko.json").read_text(encoding="utf-8")
    )
    assert data["title"] == "픽스처 대본"
    assert validate_line_timed_scenes(data)[0] == []


def test_missing_title_leaves_the_field_out(paths):
    """제목이 없으면 필드를 안 쓴다 — 빈 문자열은 계약 위반이고 부재는 아니다 (D-3)."""
    install_md(paths)
    md = paths.topic_dir(PISA) / "script.md"
    md.write_text(
        md.read_text(encoding="utf-8").replace("# 픽스처 대본\n", ""), encoding="utf-8"
    )

    run(paths)

    data = json.loads(
        (paths.run_dir(RUN_ID) / "scenes.timed.ko.json").read_text(encoding="utf-8")
    )
    assert "title" not in data
    assert validate_line_timed_scenes(data)[0] == []


def test_each_language_carries_its_own_title(paths):
    """제목도 번안본의 것이다 — 파일이 언어별이라 저절로 그렇게 된다."""
    install_md(paths)
    ja = paths.topic_dir(PISA) / "script.ja.md"
    body = (paths.topic_dir(PISA) / "script.md").read_text(encoding="utf-8")
    ja.write_text(body.replace("# 픽스처 대본", "# フィクスチャ台本"), encoding="utf-8")

    run(paths)

    run_dir = paths.run_dir(RUN_ID)
    ko = json.loads((run_dir / "scenes.timed.ko.json").read_text(encoding="utf-8"))
    jp = json.loads((run_dir / "scenes.timed.ja.json").read_text(encoding="utf-8"))
    assert ko["title"] == "픽스처 대본"
    assert jp["title"] == "フィクスチャ台本"
