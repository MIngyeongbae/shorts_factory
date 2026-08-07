import pytest

from shorts_factory.jsonio import JSONExtractionError, extract_json_object


def test_plain_json():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '설명 문장\n```json\n{"a": 1}\n```\n'
    assert extract_json_object(text) == {"a": 1}


def test_unlabeled_fence():
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_leading_prose():
    assert extract_json_object('다음은 결과입니다.\n{"a": 1}') == {"a": 1}


def test_nested_braces_are_balanced():
    text = '앞말 {"a": {"b": [1, 2]}, "c": "}"} 뒷말'
    assert extract_json_object(text) == {"a": {"b": [1, 2]}, "c": "}"}


def test_brace_inside_string_does_not_terminate():
    text = '{"claim": "성벽 {구간} 표기", "n": 1}'
    assert extract_json_object(text) == {"claim": "성벽 {구간} 표기", "n": 1}


def test_escaped_quote_inside_string():
    text = r'{"claim": "그는 \"환장할 노릇\"이라 적었다"}'
    assert extract_json_object(text)["claim"] == '그는 "환장할 노릇"이라 적었다'


@pytest.mark.parametrize("bad", ["", "   ", "JSON 없음", "[1, 2, 3]"])
def test_invalid_inputs_raise(bad):
    with pytest.raises(JSONExtractionError):
        extract_json_object(bad)
