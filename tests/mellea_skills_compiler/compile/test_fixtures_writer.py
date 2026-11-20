"""Unit tests for the deterministic fixtures_writer."""

from __future__ import annotations

import ast
import warnings

import pytest

from mellea_skills_compiler.compile.writers import fixtures_writer


TRICKY_MULTILINE_VALUES = [
    'NAME="Ubuntu"\nVERSION="22.04"',
    'line 1\n""',
    "line 1\n\\",
    "a\\nb\nc",
    "C:\\path\\file\nnext",
    "C:\\path\nnext",
    "a\rb\nc",
    "a\x00b\nc",
    'has """triple""" quotes\nand newline',
]


class TestFixturesWriterRender:
    """Test cases for fixtures_writer.render_fixture()."""

    @pytest.mark.parametrize("value", TRICKY_MULTILINE_VALUES)
    def test_multiline_edge_cases_roundtrip_without_syntax_errors_or_warnings(self, value):
        fixture = {
            "id": "tricky_strings",
            "description": value,
            "inputs": {
                "text": value,
                "nested": {"lines": [value]},
            },
        }
        source = fixtures_writer.render_fixture(fixture)
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            ast.parse(source)
            ns: dict = {}
            exec(compile(source, "<fixture>", "exec"), ns)

        inputs, fid, description = ns["make_tricky_strings"]()
        assert fid == "tricky_strings"
        assert description == value
        assert inputs == {"text": value, "nested": {"lines": [value]}}

    def test_plain_multiline_string_uses_triple_quotes(self):
        fixture = {
            "id": "plain_multiline",
            "description": "plain",
            "inputs": {"text": "line 1\nline 2"},
        }
        source = fixtures_writer.render_fixture(fixture)
        assert '"""line 1\nline 2"""' in source
