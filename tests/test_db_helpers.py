"""Tests for db.py helper functions: safe_json_loads, safe_parse_tags."""

from notely.db import safe_json_loads, safe_parse_tags


class TestSafeJsonLoads:
    def test_none_returns_empty_dict(self):
        assert safe_json_loads(None) == {}

    def test_none_with_custom_default(self):
        assert safe_json_loads(None, default=[]) == []

    def test_dict_passthrough(self):
        d = {"key": "value"}
        assert safe_json_loads(d) is d

    def test_list_passthrough(self):
        lst = [1, 2, 3]
        assert safe_json_loads(lst) is lst

    def test_valid_json_string(self):
        assert safe_json_loads('{"a": 1}') == {"a": 1}

    def test_invalid_json_string(self):
        assert safe_json_loads("not json") == {}

    def test_invalid_json_with_default(self):
        assert safe_json_loads("bad", default=[]) == []

    def test_empty_string(self):
        assert safe_json_loads("") == {}


class TestSafeParseTags:
    def test_list_passthrough(self):
        tags = ["python", "ai"]
        assert safe_parse_tags(tags) is tags

    def test_json_string(self):
        assert safe_parse_tags('["a", "b"]') == ["a", "b"]

    def test_invalid_json(self):
        assert safe_parse_tags("not json") == []

    def test_none(self):
        assert safe_parse_tags(None) == []

    def test_json_non_list(self):
        assert safe_parse_tags('{"not": "a list"}') == []

    def test_empty_string(self):
        assert safe_parse_tags("") == []
