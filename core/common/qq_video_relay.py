"""Upload media or messages to a QQ private relay and reference their sent message IDs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from astrbot.api.message_components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Video,
)


class RelayVideoReferenceNode(Node):
    """A forward node that references an already sent QQ message."""

    def toDict(self) -> dict:
        return {"type": "node", "data": {"id": int(self.id or 0)}}

    async def to_dict(self) -> dict:
        return self.toDict()


def _parse_private_umo(umo: str) -> tuple[str, int]:
    parts = str(umo or "").strip().split(":", 2)
    if len(parts) != 3:
        raise ValueError("UMO 格式应为 platform:FriendMessage:QQ号")

    platform_id, message_type, user_id = (part.strip() for part in parts)
    if not platform_id or message_type.casefold() != "friendmessage":
        raise ValueError("中转 UMO 必须是 QQ 私聊会话")
    if not user_id.isdigit():
        raise ValueError("中转 UMO 的私聊 QQ 号必须为数字")
    return platform_id, int(user_id)


def _extract_message_id(result: object) -> int:
    data = result
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    elif hasattr(data, "data"):
        data = getattr(data, "data")

    message_id = None
    if isinstance(data, dict):
        message_id = data.get("message_id")
    elif hasattr(data, "message_id"):
        message_id = getattr(data, "message_id")

    if message_id is None:
        raise ValueError("QQ 私聊发送接口没有返回 message_id")
    try:
        return int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("QQ 私聊发送接口返回了无效的 message_id") from exc


async def _component_to_relay_payload(
    event,
    component: BaseMessageComponent,
) -> dict:
    from_segment = getattr(event, "_from_segment_to_dict", None)
    if callable(from_segment):
        return await from_segment(component)

    if isinstance(component, Image):
        file_ref = str(getattr(component, "file", "") or "").strip()
        if file_ref.startswith(("http://", "https://", "base64://")):
            return {"type": "image", "data": {"file": file_ref}}
        if hasattr(component, "convert_to_base64"):
            try:
                bs64 = await component.convert_to_base64()
                if bs64:
                    return {"type": "image", "data": {"file": f"base64://{bs64}"}}
            except Exception:
                pass
        if hasattr(component, "register_to_file_service"):
            try:
                callback_url = await component.register_to_file_service()
                if callback_url:
                    return {"type": "image", "data": {"file": callback_url}}
            except Exception:
                pass
        return {
            "type": "image",
            "data": {"file": file_ref or str(getattr(component, "path", "") or "")},
        }

    if isinstance(component, File):
        payload = await component.to_dict()
        file_val = str(payload.get("data", {}).get("file", "") or "")
        if file_val and "://" not in file_val:
            try:
                path_obj = Path(file_val)
                if path_obj.is_absolute():
                    payload["data"]["file"] = path_obj.as_uri()
            except Exception:
                pass
        return payload

    if hasattr(component, "to_dict"):
        return await component.to_dict()
    return component.toDict()


async def send_components_to_private_relay(
    event,
    relay_umo: str,
    components: Sequence[BaseMessageComponent],
) -> list[RelayVideoReferenceNode]:
    """Send components sequentially to the configured QQ private chat and return ID reference nodes."""

    if not components:
        return []

    platform_id, relay_user_id = _parse_private_umo(relay_umo)
    if platform_id != str(event.get_platform_id()):
        raise ValueError("中转 UMO 与当前 QQ 消息不属于同一个平台实例")
    if str(getattr(event, "unified_msg_origin", "")) == str(relay_umo).strip():
        raise ValueError("中转 UMO 不能与当前接收会话相同")

    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        raise ValueError("当前消息平台不支持 OneBot call_action")

    routing: dict[str, str] = {}
    self_id = event.get_self_id()
    if self_id:
        routing["self_id"] = str(self_id)

    references: list[RelayVideoReferenceNode] = []
    for component in components:
        payload = await _component_to_relay_payload(event, component)
        result = await call_action(
            "send_private_msg",
            user_id=relay_user_id,
            message=[payload],
            **routing,
        )
        references.append(
            RelayVideoReferenceNode(id=_extract_message_id(result), content=[])
        )

    return references


async def send_videos_to_private_relay(
    event,
    relay_umo: str,
    videos: Sequence[Video],
) -> list[RelayVideoReferenceNode]:
    """Send videos to the configured QQ private chat and return ID reference nodes.

    Calling the adapter action directly preserves its response message ID. Each
    returned ID is tied to the exact video sent by this invocation, so concurrent
    parse tasks do not need to infer which message was sent most recently.
    """

    return await send_components_to_private_relay(event, relay_umo, videos)
