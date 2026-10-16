from __future__ import annotations

from amadeus_bot.services.feature_flags import FeatureFlagService


class GroupAdminService:
    def __init__(self, features: FeatureFlagService) -> None:
        self.features = features

    async def execute(
        self, action: str, *, bot, group_id: str | None, actor: str, target: str, value
    ) -> dict:
        if not group_id or bot is None:
            return {"success": False, "error": "群管理只能在当前群聊中使用"}
        if not self.features.status("group_admin", group_id).enabled:
            return {"success": False, "error": "当前群已关闭群管理功能"}
        if action not in {"group_card", "group_ban"}:
            return {"success": False, "error": "未知群管理操作"}
        if not target.isdigit() or int(target) <= 0:
            return {"success": False, "error": "目标 QQ 必须是正整数"}
        if action == "group_card" and (not isinstance(value, str) or len(value) > 60):
            return {"success": False, "error": "群名片最多 60 字，空字符串表示清除"}
        if action == "group_ban" and (type(value) is not int or not 0 <= value <= 30 * 86400):
            return {"success": False, "error": "禁言秒数必须在 0～2592000 之间，0 表示解除禁言"}
        try:
            members = {}
            for user_id in (str(bot.self_id), actor, target):
                if user_id not in members:
                    members[user_id] = await bot.get_group_member_info(
                        group_id=int(group_id), user_id=int(user_id), no_cache=True
                    )
                    if str(members[user_id].get("user_id")) != user_id:
                        raise ValueError("member identity mismatch")
            bot_role = members[str(bot.self_id)].get("role")
            actor_role = members[actor].get("role")
            target_role = members[target].get("role")
            if bot_role not in {"owner", "admin"}:
                return {"success": False, "error": "机器人不是本群管理员，无法执行群管理"}
            if actor_role not in {"owner", "admin"}:
                if actor_role != "member" or actor != target:
                    return {"success": False, "error": "请求者不是本群管理员，只能修改自己的群名片或禁言自己"}
                if action == "group_ban" and value == 0:
                    return {"success": False, "error": "解除禁言需要本群管理权限，不能自行解除禁言"}
            if target == str(bot.self_id) or target_role == "owner":
                return {"success": False, "error": "不能管理机器人自身或群主"}
            if target_role not in {"member", "admin"}:
                return {"success": False, "error": "目标群成员身份无效"}
            if target_role == "admin" and (bot_role != "owner" or actor_role != "owner"):
                return {"success": False, "error": "不能管理同级或更高权限的群成员"}
            if action == "group_card":
                await bot.call_api("set_group_card", group_id=int(group_id), user_id=int(target), card=value)
            else:
                await bot.call_api(
                    "set_group_ban", group_id=int(group_id), user_id=int(target), duration=value
                )
        except Exception as exc:
            return {
                "success": False,
                "error": f"QQ 群管理接口失败（{type(exc).__name__}），未确认操作成功",
            }
        return {"success": True, "target_user_id": target, "action": action}
