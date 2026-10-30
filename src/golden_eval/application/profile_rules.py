"""把字段定义与对应的已发布提取规则放在一起，避免参考自由文本覆盖字段约束。"""

import re

from ..domain.models import GoldenSharedContext

_HEADINGS = re.compile(r"(?m)^(#{1,6})[ \t]+[^\n]+")
_VALUES = re.compile(r"(?m)^\s*-\s*\{([^{}:：\n]+)[:：]")


def profile_definitions(context: GoldenSharedContext) -> list[dict]:
    fields = []
    for field in context.profile_schema:
        definition = field.model_dump(mode="json")
        rules, values = [], []
        marker = re.compile(rf"tool\.update\.visitor\.{re.escape(field.key)}(?![\w])")
        for skill in context.skill_rules:
            instruction = skill.get("instruction", "")
            found = marker.search(instruction)
            if found is None:
                continue
            position = found.start()
            headings = list(_HEADINGS.finditer(instruction))
            preceding = [heading for heading in headings if heading.start() <= position]
            if not preceding:
                continue  # 无字段小节时仍可从 shared_context.skill_rules 核对全文。
            heading = preceding[-1]
            end = next((item.start() for item in headings
                        if item.start() > position and len(item[1]) <= len(heading[1])),
                       len(instruction))
            section = instruction[heading.start():end].strip()
            section_fields = set(re.findall(r"tool\.update\.visitor\.([A-Za-z0-9_]+)", section))
            if section_fields != {field.key}:
                # 共用章节不能整段归给一个字段，否则会套入其他字段（如最终成交价）的约束。
                start = instruction.rfind("\n", 0, position) + 1
                end = instruction.find("\n", position)
                section = instruction[start:end if end >= 0 else len(instruction)].strip()
            rules.append({"skill": skill.get("name", ""), "rule": section})
            # 只有明确列举的选项才是约束；字段说明中的“如/例如”不能当作封闭枚举。
            if "可选值及定义" in section and section_fields == {field.key}:
                values.extend(value.strip() for value in _VALUES.findall(section))
        if rules:
            definition["extraction_rules"] = rules
        if values:
            definition["allowed_values"] = list(dict.fromkeys(values))
        fields.append(definition)
    return fields
