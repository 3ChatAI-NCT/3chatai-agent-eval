from pydantic import SecretStr

from agent_eval import profile_labels
from agent_eval.config import Settings
from golden_eval.domain.corpus import ProfileFieldDefinition

SPACE = {"id": "rec1", "source_workspace_id": "src", "target_workspace_id": "eval"}


def settings():
    return Settings(database_url=SecretStr("postgresql://unused"), environment="development",
                    dev_identity=True, platform_base_url="http://gateway.test")


def test_fields_come_from_the_space_profile_configuration_and_are_cached(monkeypatch):
    profile_labels._cache.clear()
    reads = []

    class Reader:
        def __init__(self, **kwargs):
            reads.append(kwargs["identity"].tenant_id)

        def defined_fields(self):
            return [ProfileFieldDefinition(key="renovation_progress_c", label="装修进度",
                                           description="记录装修施工阶段"),
                    ProfileFieldDefinition(key="raw_c", label="raw_c")]

    monkeypatch.setattr(profile_labels, "credentials", lambda *_args: ("token", "target"))
    monkeypatch.setattr(profile_labels, "PlatformProfileReader", Reader)
    fields = profile_labels.profile_fields(settings(), SPACE)
    assert fields["renovation_progress_c"] == {"label": "装修进度",
                                               "description": "记录装修施工阶段"}
    assert fields["phoneNumber"]["label"] == "手机号"
    assert fields["raw_c"]["label"] == ""  # 没有中文名的字段，界面直接显示字段键
    profile_labels.profile_fields(settings(), SPACE)
    assert reads == ["src"]


def test_unreadable_profile_fields_fall_back_to_built_in_names(monkeypatch):
    profile_labels._cache.clear()

    def unavailable(*_args):
        raise ValueError("飞书台账中找不到与平台登记一致的空间")

    monkeypatch.setattr(profile_labels, "credentials", unavailable)
    assert profile_labels.profile_fields(settings(), SPACE) == profile_labels.BUILT_IN
