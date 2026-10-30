from test_jsonl_corpus import shared_payload

from golden_eval.application.profile_rules import profile_definitions
from golden_eval.domain.models import GoldenShared


def test_explicit_skill_options_override_examples_without_mixing_fields():
    payload = shared_payload()
    payload['context']['skill_rules'] = [{'name': '标签', 'instruction': '''
## 城市 {{tool.update.visitor.city}}
可选值及定义：
- {成都:客户在成都}
- {北京:客户在北京}
## 预算 {{tool.update.visitor.budget}}
可选值及定义：
- {高:预算高}
'''}]
    definition = profile_definitions(GoldenShared.model_validate(payload).context)[0]
    assert definition['allowed_values'] == ['成都', '北京']
    assert '预算' not in definition['extraction_rules'][0]['rule']


def test_examples_in_description_do_not_create_closed_options():
    payload = shared_payload()
    payload['context']['profile_schema'][0]['description'] = '城市，例如成都、北京等'
    definition = profile_definitions(GoldenShared.model_validate(payload).context)[0]
    assert 'allowed_values' not in definition


def test_multi_field_section_does_not_attach_other_field_restrictions():
    payload = shared_payload()
    payload['context']['skill_rules'] = [{'name': '提取', 'instruction': '''
## 画像更新
- 客户所在城市写入 {{tool.update.visitor.city}}
- 成交价仅在客户接受后写入 {{tool.update.visitor.final_price_c}}
'''}]
    definition = profile_definitions(GoldenShared.model_validate(payload).context)[0]
    assert '成交价' not in definition['extraction_rules'][0]['rule']


def test_judge_classifies_enum_fields_from_rules_without_reference_value_anchor(tmp_path):
    from test_judge_model import _evaluate_run, _judge_prompt
    from test_judge_report import golden_session, observation

    from golden_eval.application.judge_report import build_worklist

    payload = shared_payload()
    payload['context']['skill_rules'] = [{'name': '城市标签', 'instruction': '''
## 城市 {{tool.update.visitor.city}}
可选值及定义：
- {成都:客户在成都市}
- {北京:客户在北京市}
'''}]
    session = golden_session()
    before = session.model_dump(mode='json')
    worklist = build_worklist(run=_evaluate_run(), sessions={session.session_id: session},
        observations=[observation()], prompt=_judge_prompt(tmp_path),
        shared=GoldenShared.model_validate(payload))
    golden = worklist['items'][0]['input']['golden']
    assert 'city' in golden['profile_required_fields']
    assert 'city' not in golden['profile_update']
    assert 'city' not in golden['profile_snapshot']
    assert session.model_dump(mode='json') == before
