from agent_eval.privacy import safe_view


def test_browser_projection_removes_credentials_and_preserves_business_content():
    raw = {"accessToken": "server-secret", "nested": {"api_key": "another-secret",
           "message": "13800138000 jane@example.com https://internal.example/path",
           "authorization": "Bearer sample-credential-123456"}}
    projected = safe_view(raw)
    assert "accessToken" not in projected
    assert "api_key" not in projected["nested"]
    assert "authorization" not in projected["nested"]
    assert projected["nested"]["message"] == raw["nested"]["message"]
    assert raw["accessToken"] == "server-secret"


def test_projection_preserves_multimodal_urls_and_contact_consistency():
    photo = "https://assets.example.com/photo.png?version=2&signature=media-signature"
    contact = "13800138000 jane@example.com"
    raw = {"request": {"input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": contact},
            {"type": "input_image", "image_url": photo}]},
        {"role": "assistant", "content": contact}]},
        "profile_snapshot": {"phone": "13800138000", "email": "jane@example.com"},
        "guidance": f"联系示例 {contact}，图片 {photo}"}
    assert safe_view(raw) == raw


def test_projection_still_removes_inline_credentials():
    assert safe_view("Bearer sample-credential-123456 sk-sample-secret-123456") == (
        "[凭据] [密钥]"
    )


def test_browser_projection_preserves_opaque_resource_ids():
    resource_id = "draft_b82cb13800138000af38ace121f4d70f"
    assert safe_view(resource_id) == resource_id
    assert safe_view(f"手机 13800138000，资源 {resource_id}") == (
        f"手机 13800138000，资源 {resource_id}"
    )


def test_customer_images_and_contact_text_keep_their_original_values():
    from agent_eval.api import _atomic_case

    photo, sticker = "https://test-media-bucket.oss-cn-beijing.aliyuncs.com/w/1.jpg", "https://wework.qpic.cn/a/0"
    call = {"call_id": "req-1:attempt:1", "workspace_id": "ws-1", "request_id": "req-1",
            "request": {"model": "m", "input": [
                {"role": "user", "content": [{"type": "input_image", "image_url": photo},
                                             {"type": "input_text", "text": "看下 13800138000"}]},
                {"role": "assistant", "content": "好的"},
                {"role": "user", "content": [{"type": "input_image", "image_url": sticker}]}]},
            "response": {"status": "completed", "output": []}}
    case = _atomic_case(call)
    assert (case["current_turn"], case["current_images"]) == ("", [sticker])
    first = case["prior_turns"][0]
    assert first["images"] == [photo] and "13800138000" in first["text"]
    assert "[客户发送了图片]" not in first["text"]


def test_evidence_projection_keeps_bodies_and_filters_embedded_json_credentials():
    import json

    from agent_eval.privacy import evidence_view

    contact = '13800138000 jane@example.com https://example.com/a?Signature=image-signature'
    raw = {'request_body': {'input': contact, 'headers': {'X-Api-Key': 'hidden-key'}},
           'raw_response': json.dumps({'text': contact, 'accessToken': 'hidden-token'}),
           'raw_agent_logs': [{'account_id': 'eval-contact', 'text': contact}],
           'candidate_api_key': 'sealed-secret', 'lease_token': 'lease-secret'}
    result = evidence_view(raw)
    assert result['request_body'] == {'input': contact, 'headers': {}}
    assert json.loads(result['raw_response']) == {'text': contact}
    assert result['raw_agent_logs'] == raw['raw_agent_logs']
    assert 'secret' not in json.dumps(result) and 'hidden' not in json.dumps(result)
    assert 'hidden-token' in raw['raw_response']
