"""Execute one Turn against the target Workspace and record what actually happened.

The lifecycle is deliberately explicit and ordered: create an isolated contact, verify its routing,
send the conversation, then read the profile back. Any failure is recorded as a partial observation
with the stage it reached, so a run never looks more complete than it is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..domain.models import (
    ContactLifecycleStage,
    ConversationReply,
    ObservationStatus,
    ToolDiagnostics,
    TurnObservation,
    TurnTask,
    new_observation_id,
)
from ..domain.tool_diagnostics import (
    bounded_raw_logs,
    build_tool_diagnostics,
    dedupe_calls,
    normalize_agent_logs,
    tool_names,
)
from ..integrations.platform_http import PlatformHTTPError
from ..ports import TargetContactPort

# The teacher-forcing projection has no routing evidence, so it carries these placeholders. Treating
# them as a real route would make us write junk into the target contact.
ROUTE_PLACEHOLDERS = frozenset({"", "unbound", "none", "null"})

# 画像提取异步执行：实测写入可晚于回复约24秒，期间空值或部分字段也会连续相同。
# 旧评测完整观察90秒；新评测可冻结稳定窗口策略，暂时稳定不能证明异步任务已完成。
DEFAULT_READBACK_TIMEOUT_SECONDS = 90.0
DEFAULT_READBACK_POLL_SECONDS = 3.0
DEFAULT_READBACK_MIN_WAIT_SECONDS = 12.0
PROFILE_RESTORE_TIMEOUT_SECONDS = 5.0


class _ProfileRestoreMismatch(RuntimeError):
    """轮前画像未恢复，属于执行条件不一致，不能归为 Agent 质量失败。"""


@dataclass(frozen=True)
class ProfileReadbackPolicy:
    timeout_seconds: float = DEFAULT_READBACK_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_READBACK_POLL_SECONDS
    min_wait_seconds: float = DEFAULT_READBACK_MIN_WAIT_SECONDS
    stable_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds < 0 or self.min_wait_seconds < 0:
            raise ValueError("readback timings must not be negative")
        if self.timeout_seconds and self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive when a timeout is configured")
        if self.stable_seconds is not None and self.stable_seconds <= 0:
            raise ValueError("stable_seconds must be positive")


def _clean_route(value: str | None) -> str:
    text = (value or "").strip()
    return "" if text.lower() in ROUTE_PLACEHOLDERS else text


def execute_turn(
    task: TurnTask,
    *,
    target: TargetContactPort,
    execution_target: str,
    profile_keys: list[str] | None = None,
    bot_user_id: str = "",
    im_bot_id: str = "",
    readback: ProfileReadbackPolicy | None = None,
    sleep=time.sleep,
    replay_request_id: str | None = None,
    routing_evidence: dict | None = None,
) -> TurnObservation:
    policy = readback or ProfileReadbackPolicy()
    started = time.monotonic()
    stage = ContactLifecycleStage.PLANNED
    visitor_id: str | None = None
    observed_before: dict | None = None
    profile_after: dict | None = None
    profile_immediate_after: dict | None = None
    readback_meta: dict = {
        "strategy": "not_requested",
        "attempts": 0,
        "elapsed_ms": 0,
        "settled": False,
    }
    replies: list = []
    reply: ConversationReply | None = None
    agent_logs: list = []
    raw_agent_logs: list[dict] = []
    chat_transport = "not_attempted"
    routing_verified = False
    status = ObservationStatus.COMPLETED
    error_code: str | None = None
    error_detail: str | None = None
    request = task.to_conversation_request()
    # Batch routing wins: the golden session itself carries no routing, and the contract allows a
    # business-confirmed fixed route for a batch.
    routing_bot_user_id = _clean_route(bot_user_id) or _clean_route(task.bot_user_id)
    routing_im_bot_id = _clean_route(im_bot_id) or _clean_route(task.im_bot_id)
    routing_required = bool(routing_bot_user_id or routing_im_bot_id)
    route = routing_evidence or {}
    openapi_route = (route.get("session_id") == task.session_id
        and route.get("channel_type") == "openApi" and route.get("visitor_found") is True
        and route.get("session_type") != "group" and route.get("subject_type") != "GROUP"
        and not _clean_route(route.get("bot_user_id"))
        and not _clean_route(route.get("im_bot_id")))
    if execution_target == "platform" and not routing_required and not openapi_route:
        # Sending without a verified route would silently land the test contact in the default skill
        # group, which changes what the tested Agent can even see. Fail before creating anything.
        return TurnObservation(
            observation_id=new_observation_id(),
            run_id=task.run_id,
            session_id=task.session_id,
            turn_id=task.turn_id,
            seq=task.seq,
            repeat_index=task.repeat_index,
            status=ObservationStatus.EXECUTION_ERROR,
            stage_reached=ContactLifecycleStage.PLANNED,
            account_id=task.account_id,
            execution_target="platform",
            routing_evidence=route,
            request_messages=list(request.messages),
            profile_before=dict(task.profile_before),
            error_code="contact_routing_missing",
            error_detail=(
                "no contact routing evidence for this session and no batch route configured; "
                "collect the session from a snapshot that carries botUserId/imBotId, or set the "
                "batch route on the target connection"
            ),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        visitor_id = target.create_contact(
            account_id=task.account_id, profile=dict(task.profile_before)
        )
        stage = ContactLifecycleStage.CREATED

        if routing_required:
            target.update_routing(
                visitor_id=visitor_id,
                account_id=task.account_id,
                bot_user_id=routing_bot_user_id,
                im_bot_id=routing_im_bot_id,
            )

        contact = target.read_contact(account_id=task.account_id)
        # 路由写入后的首次读取可能还没有字段；只回读缺失值，不重写联系人，也不容忍明确错配。
        routing_started = time.monotonic()
        expected_route = {"botUserId": routing_bot_user_id, "imBotId": routing_im_bot_id}
        while routing_required and any(
            expected and not contact.get(key) for key, expected in expected_route.items()
        ):
            if any(expected and contact.get(key) and str(contact[key]) != expected
                   for key, expected in expected_route.items()):
                break
            remaining = 5.0 - (time.monotonic() - routing_started)
            if remaining <= 0:
                break
            sleep(min(0.5, remaining))
            contact = target.read_contact(account_id=task.account_id)
        # OpenAPI 联系人创建后渠道也可能暂不可见；仅在渠道和机器人字段均缺失时回读。
        while (not routing_required and openapi_route and not contact.get("channelId.type")
               and not _clean_route(contact.get("botUserId"))
               and not _clean_route(contact.get("imBotId"))):
            remaining = 5.0 - (time.monotonic() - routing_started)
            if remaining <= 0:
                break
            sleep(min(0.5, remaining))
            contact = target.read_contact(account_id=task.account_id)
        observed_before = _profile_snapshot(
            contact, profile_keys or list(task.profile_before)
        )
        # 机器人路由核对 ID；来源已确认的 OpenAPI 路由另核对目标渠道及机器人字段为空。
        routing_verified = routing_required and _routing_matches(
            contact, bot_user_id=routing_bot_user_id, im_bot_id=routing_im_bot_id
        )
        if not routing_required and openapi_route:
            routing_verified = (contact.get("channelId.type") == "openApi"
                and not _clean_route(contact.get("botUserId"))
                and not _clean_route(contact.get("imBotId")))
            if not routing_verified:
                raise RuntimeError(
                    "OpenAPI source route does not match the target contact route: "
                    f"observed channelId.type={contact.get('channelId.type')!r} "
                    f"botUserId={contact.get('botUserId')!r} "
                    f"imBotId={contact.get('imBotId')!r}"
                )
        if not routing_verified and routing_required:
            raise RuntimeError(
                "routing verification failed: "
                f"expected botUserId={routing_bot_user_id!r} imBotId={routing_im_bot_id!r}, "
                f"observed botUserId={contact.get('botUserId')!r} "
                f"imBotId={contact.get('imBotId')!r}"
            )
        stage = ContactLifecycleStage.ROUTING_VERIFIED

        # 创建成功不证明画像已可见；先核对冻结的非空初值，再发聊天。
        expected_profile = {key: value for key, value in task.profile_before.items()
                            if _normalise_profile_value(value) not in ("", None, [], {})}
        restore_started = time.monotonic()
        while expected_profile:
            differences = {key for key, expected in expected_profile.items()
                           if _normalise_profile_value(observed_before.get(key))
                           != _normalise_profile_value(expected)}
            if not differences:
                break
            wrong = {key for key in differences
                     if _normalise_profile_value(observed_before.get(key))
                     not in ("", None, [], {})}
            remaining = PROFILE_RESTORE_TIMEOUT_SECONDS - (time.monotonic() - restore_started)
            if wrong or remaining <= 0:
                raise _ProfileRestoreMismatch(
                    "轮前画像恢复不一致，未发送聊天；字段：" + ", ".join(sorted(differences)))
            sleep(min(0.5, remaining))
            remaining = PROFILE_RESTORE_TIMEOUT_SECONDS - (time.monotonic() - restore_started)
            if remaining <= 0:
                continue
            try:
                contact = target.read_contact(account_id=task.account_id, timeout_seconds=remaining)
                observed_before = _profile_snapshot(
                    contact, profile_keys or list(task.profile_before))
            except (PlatformHTTPError, ValueError):
                # 仅回读短暂缺失；不重写联系人，也不挤占聊天之后的90秒观察预算。
                continue

        reply = target.send_messages(
            account_id=task.account_id, messages=list(request.messages),
            request_id=replay_request_id,
        )
        replies = list(reply.messages)
        raw_agent_logs = list(reply.agent_logs)
        chat_transport = reply.chat_transport
        agent_logs = normalize_agent_logs(raw_agent_logs)
        stage = ContactLifecycleStage.RESPONSE_OBSERVED

        keys = profile_keys or list(task.profile_before)
        readback_started = time.monotonic()
        initial_errors = []
        try:
            immediate = target.read_contact(account_id=task.account_id,
                timeout_seconds=min(policy.poll_seconds, policy.timeout_seconds)
                if policy.timeout_seconds else None)
            profile_immediate_after = _profile_snapshot(immediate, keys)
        except (PlatformHTTPError, ValueError) as exc:
            # 回复已成功返回；画像读取异常不能把本轮回复改成执行失败。
            initial_errors.append(str(exc)[:200])
        profile_after, readback_meta = _settle_profile(
            target,
            account_id=task.account_id,
            keys=keys,
            baseline=profile_immediate_after,
            policy=policy,
            sleep=sleep,
            started_at=readback_started,
        )
        readback_meta["errors"] = initial_errors + readback_meta["errors"]
        if readback_meta["settled"]:
            stage = ContactLifecycleStage.PROFILE_SETTLED
    except _ProfileRestoreMismatch as exc:
        status = ObservationStatus.EXECUTION_ERROR
        error_code = "profile_restore_mismatch"
        error_detail = str(exc)
    except PlatformHTTPError as exc:
        chat_failed = stage is ContactLifecycleStage.ROUTING_VERIFIED
        # 网关超时或上游错误不能证明 Agent 未执行；保留投递未知，避免据此重发。
        delivery_unknown = chat_failed and (exc.status is None or exc.status == 408
                                           or exc.status >= 500)
        status = (
            ObservationStatus.DELIVERY_UNKNOWN
            if delivery_unknown
            else ObservationStatus.EXECUTION_ERROR
        )
        if chat_failed:
            chat_transport = "sync_delivery_unknown" if delivery_unknown else "sync_failed"
        error_code = f"platform_http_{exc.status or 'network'}"
        error_detail = exc.detail[:500]
    except (RuntimeError, ValueError) as exc:
        status = ObservationStatus.EXECUTION_ERROR
        error_code = "contact_lifecycle_error"
        error_detail = str(exc)[:500]

    return TurnObservation(
        observation_id=new_observation_id(),
        run_id=task.run_id,
        session_id=task.session_id,
        turn_id=task.turn_id,
        seq=task.seq,
        repeat_index=task.repeat_index,
        status=status,
        stage_reached=stage,
        account_id=task.account_id,
        execution_target="platform" if execution_target == "platform" else "fixture",
        request_messages=list(request.messages),
        response_messages=list(replies),
        profile_before=dict(task.profile_before),
        profile_observed_before=observed_before,
        profile_immediate_after=profile_immediate_after,
        profile_readback=readback_meta,
        profile_after=profile_after,
        profile_writes=_profile_writes(observed_before, profile_after),
        bot_user_id=routing_bot_user_id or None,
        im_bot_id=routing_im_bot_id or None,
        routing_verified=routing_verified,
        routing_evidence=route,
        chat_transport=chat_transport,
        tool_diagnostics=_tool_diagnostics(task, raw_agent_logs),
        agent_logs=agent_logs,
        raw_agent_logs=bounded_raw_logs(raw_agent_logs),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error_code=error_code,
        error_detail=error_detail,
        # 发送失败或投递未知时也记下 ID：请求可能已到达网关，链路仍可按它追溯
        replay_request_id=replay_request_id,
        replay_request_id_echoed=reply.request_id_echoed if reply else None,
    )


def _tool_diagnostics(task: TurnTask, raw_agent_logs: list[dict]) -> ToolDiagnostics:
    """6.2 diagnostic: golden tool names vs the Agent's own log. Never scored, never gated."""

    expected = [call.name for call in task.expected.tool_calls]
    actual = tool_names(raw_agent_logs)
    return build_tool_diagnostics(
        expected,
        actual,
        expected_refs=len(expected),
        actual_refs=len(dedupe_calls(raw_agent_logs)),
    )


def _profile_snapshot(contact: dict, keys: list[str]) -> dict:
    extend = contact.get("extendMap") if isinstance(contact.get("extendMap"), dict) else {}
    snapshot: dict = {}
    for key in keys:
        if key in contact:
            snapshot[key] = contact[key]
        elif key in extend:
            snapshot[key] = extend[key]
        else:
            snapshot[key] = ""
    return snapshot


def _normalise_profile_value(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return [_normalise_profile_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise_profile_value(item) for key, item in value.items()}
    return value


def _routing_matches(contact: dict, *, bot_user_id: str, im_bot_id: str) -> bool:
    if bot_user_id and str(contact.get("botUserId") or "") != bot_user_id:
        return False
    if im_bot_id and str(contact.get("imBotId") or "") != im_bot_id:
        return False
    return True


def _settle_profile(
    target: TargetContactPort,
    *,
    account_id: str,
    keys: list[str],
    baseline: dict | None,
    policy: ProfileReadbackPolicy,
    sleep,
    started_at: float | None = None,
) -> tuple[dict | None, dict]:
    """Poll the contact until the profile settles or the bounded window closes.

    Returns the best snapshot seen plus its metadata. A read failure keeps the last good snapshot
    and records the error: it never upgrades "we could not read it" into "nothing was written".
    """

    started = time.monotonic() if started_at is None else started_at
    attempts = 1
    latest = baseline
    errors: list[str] = []
    stable: dict | None = None
    previous_read_ok = baseline is not None
    stable_since = time.monotonic() if previous_read_ok else None
    exit_reason = "deadline"

    if policy.stable_seconds is None and policy.timeout_seconds and policy.min_wait_seconds:
        remaining = max(0, policy.timeout_seconds - (time.monotonic() - started))
        sleep(min(policy.min_wait_seconds, remaining))

    while True:
        elapsed = time.monotonic() - started
        if not policy.timeout_seconds or elapsed >= policy.timeout_seconds:
            break
        sleep(min(policy.poll_seconds, policy.timeout_seconds - elapsed))
        remaining = policy.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        attempts += 1
        try:
            current = _profile_snapshot(target.read_contact(
                account_id=account_id, timeout_seconds=(min(policy.poll_seconds, remaining)
                    if policy.stable_seconds is not None else remaining)), keys)
        except (PlatformHTTPError, ValueError) as exc:
            errors.append(str(exc)[:200])
            stable = None
            previous_read_ok = False
            stable_since = None
            continue
        observed_at = time.monotonic()
        stable = current if previous_read_ok and current == latest else None
        if stable is None:
            stable_since = observed_at
        latest = current
        previous_read_ok = True
        # 新口径按真实回读的稳定时间提前结束，不以参考答案或期望字段决定等待时间。
        if (policy.stable_seconds is not None and stable is not None
                and observed_at - started >= policy.min_wait_seconds
                and stable_since is not None
                and observed_at - stable_since >= policy.stable_seconds):
            exit_reason = "stable_window"
            break

    settled = stable is not None and (policy.stable_seconds is None or (
        stable_since is not None and time.monotonic() - stable_since >= policy.stable_seconds))
    metadata = {
        "strategy": "stable_window" if policy.stable_seconds is not None else "bounded_poll",
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "settled": settled,
        "completion_verified": False,
        "timeout_seconds": policy.timeout_seconds,
        "poll_seconds": policy.poll_seconds,
        "min_wait_seconds": policy.min_wait_seconds,
        "stable_seconds": policy.stable_seconds,
        "exit_reason": exit_reason,
        "errors": errors,
    }
    return (stable if settled else latest), metadata


def _profile_writes(before: dict | None, after: dict | None) -> dict:
    """Derive writes deterministically from the observed difference, never from the reply text."""

    if before is None or after is None:
        return {}
    writes: dict = {}
    for key, value in after.items():
        previous = before.get(key)
        if value in ("", None, [], {}):
            continue
        if previous != value:
            writes[key] = {"value": value, "updated_from": previous}
    return writes
