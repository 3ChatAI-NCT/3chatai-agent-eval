"""平台日志里的画像写入解析：只有 `internalTool: visitor` 才算「直接拉取到的」画像值。"""

from __future__ import annotations

from golden_eval.domain.profile_writes import (
    parse_write_value,
    profile_writes_from_logs,
)


def _visitor_log(message: str, *, name: str = "visitor", log_type: str = "internalTool") -> dict:
    return {
        "type": log_type,
        "name": name,
        "message": message,
        "entity": {"name": "visitor", "values": [message]},
    }


def test_visitor_internal_tool_writes_become_field_values() -> None:
    """线上返回的真实形状：一条日志一个字段，`字段: "" -> "值"`。"""

    writes = profile_writes_from_logs(
        [
            {"type": "knowledge", "costTime": 374},
            _visitor_log('WztPq_c: "" -> "桌椅维修"'),
            _visitor_log('dzGkB_c: "" -> "是"'),
            {"type": "agent", "name": "mainagent"},
        ]
    )

    assert writes == {"WztPq_c": "桌椅维修", "dzGkB_c": "是"}


def test_task_logs_that_echo_the_same_text_are_not_writes() -> None:
    """task 日志的 message 里也有同一段文本，但那是任务回显，不是写入记录。"""

    writes = profile_writes_from_logs(
        [
            {
                "type": "task",
                "name": "售后咨询",
                "message": 'WztPq_c: "" -> "桌椅维修"；dzGkB_c: "" -> "是"',
            },
            _visitor_log('WztPq_c: "" -> "桌椅维修"', name="其他实体"),
            {"type": "internalTool", "name": "visitor", "message": "没有箭头 的 文本"},
        ]
    )

    assert writes == {}


def test_one_message_can_carry_several_writes_and_the_last_one_wins() -> None:
    writes = profile_writes_from_logs(
        [
            _visitor_log('city: "" -> "成都"；budget: "1000" -> "2000"'),
            _visitor_log('city: "成都" -> "重庆"'),
        ]
    )

    assert writes == {"city": "重庆", "budget": "2000"}


def test_writes_can_be_read_from_entity_values_and_other_arrow_shapes() -> None:
    writes = profile_writes_from_logs(
        [
            {
                "type": "internalTool",
                "name": "visitor",
                "entity": {"values": ['name: "张三" → "李四"']},
            },
            {"type": "internalTool", "name": "visitor", "message": 'phone: "" => "13800000000"'},
        ]
    )

    assert writes == {"name": "李四", "phone": "13800000000"}


def test_clearing_a_field_parses_to_an_empty_value() -> None:
    writes = profile_writes_from_logs(
        [
            _visitor_log('note: "老备注" -> ""'),
            _visitor_log('other: "老备注" -> null'),
        ]
    )

    assert writes == {"note": "", "other": ""}


def test_write_values_keep_json_shapes_but_strip_quotes() -> None:
    assert parse_write_value('"桌椅维修"') == "桌椅维修"
    assert parse_write_value('""') == ""
    assert parse_write_value("[]") == []
    assert parse_write_value('["a", "b"]') == ["a", "b"]
    assert parse_write_value("12") == "12"
    assert parse_write_value('"说 \\"好\\" 了"') == '说 "好" 了'
