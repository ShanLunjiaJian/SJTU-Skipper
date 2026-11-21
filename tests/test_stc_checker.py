import json
from pathlib import Path

import pytest

from plugins.note_check_stc.checker import check_file

DEFAULT_AVOID = json.loads(
    (Path(__file__).parents[1] / "src/plugins/note_check_stc/avoid.json").read_text(
        encoding="utf-8"
    )
)


def check_text(tmp_path, text):
    path = tmp_path / "note.md"
    path.write_text(text, encoding="utf-8")
    return check_file(path, name="notes/lecture.md", avoid=DEFAULT_AVOID)


def test_report_contract(tmp_path):
    report = check_text(
        tmp_path, "进一步进行分析，显著赋能，不是工具而是结果。中文AI．"
    )
    assert set(report) == {"file", "errors", "warnings", "issues"}
    assert report["file"] == "notes/lecture.md"
    assert {issue["rule"] for issue in report["issues"]} >= {
        "G3",
        "G4",
        "G5",
        "G6",
        "G7",
        "G17",
        "G19",
    }
    assert report["errors"] == sum(
        issue["level"] == "error" for issue in report["issues"]
    )
    assert report["warnings"] == sum(
        issue["level"] == "warning" for issue in report["issues"]
    )
    for issue in report["issues"]:
        assert set(issue) == {
            "line",
            "col",
            "rule",
            "level",
            "match",
            "context",
            "reason",
            "suggestion",
        }
    json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize(
    "protected",
    [
        "$显著提升。中文AI$",
        "$$显著提升。中文AI$$",
        r"\(显著提升。中文AI\)",
        r"\[显著提升。中文AI\]",
        "`显著提升。中文AI`",
        "``显著`提升。中文AI``",
        "「显著提升。中文AI」",
        "『显著提升。中文AI』",
        "https://example.org/显著提升。中文AI",
    ],
)
def test_protected_spans_preserve_columns(tmp_path, protected):
    text = protected + " 显著．"
    report = check_text(tmp_path, text)
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert (issue["rule"], issue["match"], issue["line"], issue["col"]) == (
        "G5",
        "显著",
        1,
        len(protected) + 2,
    )
    assert issue["context"] == text


def test_fences_blockquotes_and_multiline_math(tmp_path):
    text = (
        "````python\n显著。\n```\n显著。\n````\n"
        "> 显著。\n  > 显著。\n"
        "$$\n显著。\n$$ 显著．\n"
        "\\[\n显著。\n\\] 显著．\n"
    )
    report = check_text(tmp_path, text)
    assert [
        (issue["line"], issue["col"], issue["match"]) for issue in report["issues"]
    ] == [
        (10, 4, "显著"),
        (13, 4, "显著"),
    ]


def test_quote_content_is_protected_but_delimiters_are_checked(tmp_path):
    report = check_text(tmp_path, '“显著。中文AI” "显著。中文AI"．')
    assert {issue["rule"] for issue in report["issues"]} == {"G17"}
    assert [issue["col"] for issue in report["issues"]] == [1, 9, 11]


def test_context_warnings_and_heading_exception(tmp_path):
    report = check_text(
        tmp_path, "# 优化方案\n部分相关实现优化．\n# 已优化方案\n进行中．"
    )
    assert [
        (issue["line"], issue["match"], issue["level"]) for issue in report["issues"]
    ] == [
        (2, "部分", "warning"),
        (2, "相关", "warning"),
        (2, "实现", "warning"),
        (2, "优化", "warning"),
        (3, "优化", "warning"),
    ]


def test_long_sentences_and_anti_echo(tmp_path):
    report = check_text(tmp_path, "甲" * 41 + "．\n（没有修改原文）已确认未使用．")
    assert [(issue["line"], issue["rule"]) for issue in report["issues"]] == [
        (1, "G12"),
        (2, "G20"),
        (2, "G20"),
    ]


def test_directives_do_not_disable_checks(tmp_path):
    report = check_text(tmp_path, "<!-- stc-disable-next-line -->\n显著．")
    assert [(issue["line"], issue["match"]) for issue in report["issues"]] == [
        (2, "显著")
    ]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_file(
            tmp_path / "missing.md", name="notes/missing.md", avoid=DEFAULT_AVOID
        )
