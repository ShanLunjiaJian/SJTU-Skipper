"""Find STC review candidates without changing source text."""

import re
from pathlib import Path

_TYPE_RULE = {
    "虚动词": "G4",
    "程度词": "G5",
    "模糊量词": "G5",
    "含糊动词": "G5",
    "营销词": "G6",
    "行话": "G6",
    "套话": "G6",
    "口号句式": "G7",
    "翻译腔": "G3",
    "冗余": "G3",
}
_CJK = r"\u4e00-\u9fff"
_ADVICE = {
    "G12": "影响理解时拆句；不得拆散定理条件或推导关系．",
    "G17": "引号使用「」和嵌套的『』；句末使用全角句点「．」．",
    "G19": "在中文与英文、数字之间加一个空格．",
    "G20": "删除无关自证和制作说明，保留必要辨析、例外与来源缺口．",
}
# Every alternative consumes input. Math and quotations can span source lines.
_PROTECTED = re.compile(
    r"<!--.*?(?:-->|\Z)|https?://[^\s<>]+|\]\([^\n)]*\)|"
    r"(?P<code>`+)(?:(?!(?P=code)).)*?(?P=code)|"
    r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<![\\$])\$(?!\$).*?(?<![\\$])\$(?!\$)|"
    r"\\\(.*?\\\)|\\\[.*?\\\]|"
    r"「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|\"[^\"\n]*\"|'[^'\n]*'",
    re.DOTALL,
)
_ANTI_ECHO = (
    (
        re.compile(
            r"（(?:不|没有?|并非|不是|也没有?)[^）\n]{0,40}）|\((?:不|没有?|并非|不是|也没有?)[^)\n]{0,40}\)"
        ),
        "括号中的排除说明需要读者依据：删掉后会造成什么具体误解",
    ),
    (
        re.compile(
            r"已(?:确认|确保)(?:不含|不包含|不涉及|没有|无)|已确认未(?:使用|采用|添加)"
        ),
        "交付结尾的自证可能多余：核对读者是否要求这项检查结果",
    ),
)


def _entries(entries):
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict)
        or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("avoid", "type", "why", "use")
        )
        for entry in entries
    ):
        raise ValueError(
            "词表必须是 JSON 数组，每项必须包含非空字符串 avoid、type、why 和 use．"
        )
    compiled = []
    for entry in entries:
        patterns = []
        for alternative in entry["avoid"].split(" / "):
            alternative = re.sub(r"（[^）]*）", "", alternative).split(" + ")[0].strip()
            if not alternative:
                continue
            if alternative.startswith("不是 X"):
                pattern = r"不是[^。．！？\n\x00]{1,30}?而是"
            elif "……" in alternative:
                head, tail = alternative.split("……", 1)
                pattern = (
                    re.escape(head) + r"[^。．！？\n\x00]{0,30}?" + re.escape(tail)
                )
            elif alternative == "进行":
                pattern = r"进行(?!中)"
            else:
                pattern = re.escape(alternative)
            patterns.append(re.compile(pattern))
        compiled.append(
            (
                entry,
                patterns,
                entry["avoid"].startswith(("部分", "优化", "实现", "相关")),
            )
        )
    return compiled


def _mask_source(text):
    chars = list(text)
    fence = None
    offset = 0
    for row in text.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", row)
        protected = fence is not None or marker is not None or re.match(r"\s*>", row)
        if fence is not None:
            if re.fullmatch(
                r" {0,3}"
                + re.escape(fence[0])
                + "{"
                + str(len(fence))
                + r",}[ \t]*[\r\n]*",
                row,
            ):
                fence = None
        elif marker:
            fence = marker[1]
        if protected:
            for index in range(offset, offset + len(row)):
                if chars[index] not in "\r\n":
                    chars[index] = "\0"
        offset += len(row)
    masked = "".join(chars)
    quote_issues = []
    for match in _PROTECTED.finditer(masked):
        value = match[0]
        start, end = match.span()
        quoted = value[0] in "「『“‘\"'"
        if quoted:
            if value[0] in "\"'" and re.search(f"[{_CJK}]", value):
                quote_issues.append((start, value))
            start += 1
            end -= 1
        elif value.startswith("]("):
            start += 1
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = "\0"
    return "".join(chars), quote_issues


def check_file(path: Path, *, name: str, avoid: list) -> dict:
    """Return heuristic review candidates with one-based source positions."""
    text = path.read_text(encoding="utf-8")
    entries = _entries(avoid)
    masked, quote_issues = _mask_source(text)
    lines = text.splitlines()
    issues = []

    def add(offset, rule, level, match, reason, suggestion=None):
        line = text.count("\n", 0, offset) + 1
        col = offset - text.rfind("\n", 0, offset)
        issues.append(
            {
                "line": line,
                "col": col,
                "rule": rule,
                "level": level,
                "match": match,
                "context": lines[line - 1] if line <= len(lines) else "",
                "reason": reason,
                "suggestion": suggestion or _ADVICE[rule],
            }
        )

    for offset, value in quote_issues:
        add(
            offset,
            "G17",
            "error",
            value,
            "中文里的引号必须使用直角引号「」和嵌套的『』",
        )

    offset = 0
    for row in masked.splitlines(keepends=True):
        for entry, patterns, soft in entries:
            rule = _TYPE_RULE.get(entry["type"], "G3")
            for pattern in patterns:
                for match in pattern.finditer(row):
                    if "\0" in match[0]:
                        continue
                    topic_heading = (
                        re.match(r"^\s*#{1,6}\s+", row)
                        and rule == "G5"
                        and match[0] in ("优化", "提升", "改善")
                        and re.fullmatch(
                            r"(评估|计划|方案|指南|手册)(?:[ \t]+#+)?[ \t\r\n]*",
                            row[match.end() :],
                        )
                        and not re.search(
                            r"已|正在|显著|大幅|明显|全面|彻底|有效|成功|自动|持续|不断|进一步|如何|怎样|怎么",
                            row[: match.start()],
                        )
                    )
                    if topic_heading:
                        continue
                    suggestion = entry["use"]
                    if rule == "G4":
                        suggestion = f"删掉「{match[0]}」，直接写后面的动作．"
                    elif rule == "G5":
                        suggestion += "．数学术语和量词按原意保留，不得编造数字"
                    add(
                        offset + match.start(),
                        rule,
                        "warning" if soft else "error",
                        match[0],
                        entry["why"],
                        suggestion,
                    )

        seen = []
        for pattern, reason in _ANTI_ECHO:
            for match in pattern.finditer(row):
                if "\0" in match[0] or any(
                    match.start() < end and match.end() > start for start, end in seen
                ):
                    continue
                seen.append(match.span())
                add(offset + match.start(), "G20", "warning", match[0], reason)
        for match in re.finditer(r"[“”‘’]|。", row):
            reason = (
                "句末必须使用全角句点「．」，不得使用 U+3002"
                if match[0] == "。"
                else "引号必须使用直角引号「」和嵌套的『』，不得使用弯引号"
            )
            add(offset + match.start(), "G17", "error", match[0], reason)
        for match in re.finditer(f"[{_CJK}][A-Za-z0-9]|[A-Za-z0-9][{_CJK}]", row):
            add(
                offset + match.start(),
                "G19",
                "warning",
                match[0],
                "中文与英文、数字之间宜空一格",
            )
        for sentence in re.finditer(r"[^。．！？；|\n\x00]+", row):
            length = len(re.findall(f"[{_CJK}]|[A-Za-z0-9]+", sentence[0]))
            if length > 40:
                add(
                    offset + sentence.start(),
                    "G12",
                    "warning",
                    sentence[0].strip()[:12] + "……",
                    f"这一句 {length} 个字，宜不超过 40 个字",
                )
        offset += len(row)

    issues.sort(key=lambda issue: (issue["line"], issue["col"]))
    return {
        "file": name,
        "errors": sum(issue["level"] == "error" for issue in issues),
        "warnings": sum(issue["level"] == "warning" for issue in issues),
        "issues": issues,
    }
