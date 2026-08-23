"""끝 프레임 OCR 게이트 (스펙 05 `[7]` ①, ADR-0056 결정 6).

규칙은 둘뿐이다 — `info` 씬은 "라벨이 전부 읽히고 계약에 없는 숫자가 없다", 일반 씬은
"글자가 없다". 구현체(tesseract)는 PATH에 있을 때만 쓰고, 없으면 게이트가 빠진다.
"""

import subprocess
from pathlib import Path

import pytest

from shorts_factory.video.ocr import (
    FakeOCR,
    OCRError,
    TesseractOCR,
    check_labels,
    check_no_text,
    detect_ocr,
    gate,
    normalize,
)


# --- 라벨 대조 -------------------------------------------------------------------


def test_labels_match_whitespace_and_case_insensitively():
    verdict = check_labels("4 MM\n22cm", ["4mm", "22 cm"])
    assert verdict.passed and verdict.missing_labels == ()


def test_missing_label_fails():
    verdict = check_labels("4mm", ["4mm", "22cm"])
    assert not verdict.passed
    assert verdict.missing_labels == ("22cm",)
    assert "22cm" in verdict.reasons[0]


def test_number_outside_the_labels_fails():
    """계약에 없는 숫자 — 모델이 지어낸 수치가 화면에 나가는 사고다."""
    verdict = check_labels("4mm 1989", ["4mm"])
    assert not verdict.passed
    assert verdict.stray_numbers == ("1989",)


def test_numbers_inside_a_label_are_not_stray():
    verdict = check_labels("Short sag 22 cm", ["Short sag", "22 cm"])
    assert verdict.passed and verdict.stray_numbers == ()


def test_words_without_digits_are_not_stray_numbers():
    """숫자 게이트는 숫자만 본다 — 단어 오인식은 비전 검수 몫이다."""
    verdict = check_labels("4mm hole", ["4mm"])
    assert verdict.passed


# --- 글자 금지 -------------------------------------------------------------------


def test_empty_frame_passes_no_text():
    assert check_no_text("").passed
    assert check_no_text("  \n ").passed


def test_any_word_fails_no_text():
    verdict = check_no_text("HOOVER DAM")
    assert not verdict.passed and "HOOVER" in verdict.stray_text


def test_single_character_noise_is_ignored():
    """질감 위의 tesseract 잡음(`|`, `l`)은 글자로 세지 않는다."""
    assert check_no_text("| l .").passed


def test_gate_dispatches_on_labels():
    assert gate("4mm", ["4mm"]).passed
    assert not gate("4mm", None).passed
    assert gate("", []).passed


def test_verdict_record_is_json_friendly():
    record = check_labels("4mm 77", ["4mm"]).record()
    assert record == {
        "passed": False, "text": "4mm 77", "missing_labels": [],
        "stray_numbers": ["77"], "stray_text": [], "reasons": record["reasons"],
    }


def test_normalize():
    assert normalize(" 4 Mm ") == "4mm"


# --- 구현체 ------------------------------------------------------------------------


def test_detect_returns_none_without_tesseract(monkeypatch):
    monkeypatch.setattr("shorts_factory.video.ocr.shutil.which", lambda _n: None)
    assert detect_ocr() is None


def test_detect_returns_tesseract_when_on_path(monkeypatch):
    monkeypatch.setattr("shorts_factory.video.ocr.shutil.which", lambda _n: "/usr/bin/tesseract")
    assert isinstance(detect_ocr(), TesseractOCR)


def test_tesseract_runner_reads_stdout(tmp_path):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "4 mm\n", "")

    text = TesseractOCR("tess", runner=runner).read(tmp_path / "end.jpg")
    assert text == "4 mm\n"
    assert calls[0][:3] == ["tess", str(tmp_path / "end.jpg"), "stdout"]


def test_tesseract_failure_is_an_ocr_error(tmp_path):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    with pytest.raises(OCRError, match="boom"):
        TesseractOCR("tess", runner=runner).read(tmp_path / "end.jpg")


def test_fake_ocr_matches_on_file_name():
    fake = FakeOCR({"3-1-end": "4mm"}, default="")
    assert fake.read(Path("clip_review/3-1-end.jpg")) == "4mm"
    assert fake.read(Path("clip_review/4-1-end.jpg")) == ""
    assert len(fake.calls) == 2
