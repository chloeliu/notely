"""Tests for vectors.py escape helper."""

from notely.vectors import _escape_where_value


class TestEscapeWhereValue:
    def test_plain_string(self):
        assert _escape_where_value("clients") == "clients"

    def test_double_quotes_escaped(self):
        assert _escape_where_value('foo"bar') == 'foo\\"bar'

    def test_backslash_escaped(self):
        assert _escape_where_value("foo\\bar") == "foo\\\\bar"

    def test_injection_attempt(self):
        malicious = 'foo" OR 1=1 --'
        escaped = _escape_where_value(malicious)
        assert '"' not in escaped.replace('\\"', '')
