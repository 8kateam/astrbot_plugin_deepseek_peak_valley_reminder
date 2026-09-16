import random
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

ONE_BOT_PLATFORM = "aiocqhttp"
DEFAULT_TZ_OFFSET = 8
MISFIRE_GRACE_TIME = 3600
COMPENSATE_WINDOW_SECONDS = 1800

SWITCH_POINTS = [
    ("peak_morning_start", 9, 0, "peak"),
    ("valley_noon_start", 12, 0, "valley"),
    ("peak_afternoon_start", 14, 0, "peak"),
    ("valley_evening_start", 18, 0, "valley"),
]

DS_DEFAULT_PEAK = (
    "⚡ 当前为峰时计价（{time}）\n"
    "峰时时段：9:00-12:00, 14:00-18:00\n"
    "调用 API 费用较高。"
)
DS_DEFAULT_VALLEY = (
    "💰 当前为谷时计价（{time}）\n"
    "谷时时段：12:00-14:00, 18:00-次日9:00\n"
    "调用 API 费用较低。"
)
DS_DEFAULT_WEEKEND = (
    "💰 当前为谷时计价（{time}）\n"
    "今天是周末，DeepSeek API 全天按谷时计价，费用优惠。"
)


@register(
    "Deepseek 峰谷时段提醒",
    "Midnight-2004",
    "用于提醒 Deepseek 现在是峰时计价还是谷时计价。",
    "0.2.2",
)
class DeepseekPeakValleyReminder(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None

    async def initialize(self):
        if not self.config.get("enable", True):
            logger.info("Deepseek 峰谷提醒插件已禁用")
            return

        tz = self._get_tz()
        self.scheduler = AsyncIOScheduler(timezone=tz)

        remind_points = self.config.get("remind_points", {}) or {}
        for point_key, hour, minute, msg_type in SWITCH_POINTS:
            if not remind_points.get(point_key, True):
                continue
            job_kwargs = {
                "hour": hour,
                "minute": minute,
                "misfire_grace_time": MISFIRE_GRACE_TIME,
            }
            if self._weekend_valley_enabled():
                job_kwargs["day_of_week"] = "mon-fri"
            self.scheduler.add_job(
                self._send_point, "cron", args=[msg_type, point_key], **job_kwargs
            )

        self.scheduler.start()
        logger.info("Deepseek 峰谷提醒调度器已启动")
        await self._compensate_missed()

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            logger.info("Deepseek 峰谷提醒调度器已停止")

    def _get_tz(self):
        tz_offset = self.config.get("timezone", DEFAULT_TZ_OFFSET)
        if not isinstance(tz_offset, int) or not (-12 <= tz_offset <= 14):
            logger.warning(
                f"timezone 配置值 {tz_offset} 无效（有效范围 -12 ~ 12），"
                f"已回退为默认值 {DEFAULT_TZ_OFFSET}"
            )
            tz_offset = DEFAULT_TZ_OFFSET
        return timezone(timedelta(hours=tz_offset))

    def _weekend_valley_enabled(self) -> bool:
        return bool(self.config.get("enable_weekend_valley", True))

    def _is_weekend(self, now: datetime) -> bool:
        return now.weekday() >= 5

    def _get_status(self, now: datetime) -> tuple:
        if self._weekend_valley_enabled() and self._is_weekend(now):
            return ("valley", "")
        h = now.hour
        if 9 <= h < 12:
            return ("peak", "peak_morning_start")
        elif 12 <= h < 14:
            return ("valley", "valley_noon_start")
        elif 14 <= h < 18:
            return ("peak", "peak_afternoon_start")
        else:
            return ("valley", "valley_evening_start")

    def _pick_message(self, messages: list) -> str:
        if not messages:
            return ""
        now = datetime.now(self._get_tz())
        time_str = now.strftime("%H:%M")
        msg = random.choice(messages)
        return msg.replace("{time}", time_str).replace("\\n", "\n")

    def _pick_or_default(self, cfg_key: str, default: str) -> str:
        messages = self.config.get(cfg_key, []) or []
        msg = self._pick_message(messages)
        return msg if msg else self._pick_message([default])

    def _sent_kv_key(self, point_key: str) -> str:
        now = datetime.now(self._get_tz())
        return f"ds_pvr_sent_{now.strftime('%Y%m%d')}_{point_key}"

    async def _send_point(self, msg_type: str, point_key: str):
        try:
            cfg_key = (
                "peak_start_message" if msg_type == "peak" else "valley_start_message"
            )
            msg = self._pick_message(self.config.get(cfg_key, []) or [])
            if msg:
                await self._send_to_targets(msg)
            await self.put_kv_data(self._sent_kv_key(point_key), True)
        except Exception as e:
            logger.error(f"切换点提醒({point_key})发送失败: {e}")

    async def _compensate_missed(self):
        try:
            now = datetime.now(self._get_tz())
            if self._weekend_valley_enabled() and self._is_weekend(now):
                return
            remind_points = self.config.get("remind_points", {}) or {}
            for point_key, hour, minute, msg_type in SWITCH_POINTS:
                point_time = now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                delta = (now - point_time).total_seconds()
                if not (0 < delta <= COMPENSATE_WINDOW_SECONDS):
                    continue
                if not remind_points.get(point_key, True):
                    continue
                if await self.get_kv_data(self._sent_kv_key(point_key), False):
                    continue
                logger.info(f"检测到刚错过的切换点 {point_key}，执行补发")
                await self._send_point(msg_type, point_key)
                break
        except Exception as e:
            logger.warning(f"启动补偿检查失败: {e}")

    def _get_aiocqhttp_clients(self) -> list:
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )
        except ImportError:
            logger.warning("无法导入 AiocqhttpAdapter")
            return []

        try:
            platforms = self.context.platform_manager.get_insts()
        except Exception as e:
            logger.warning(f"无法获取平台实例列表: {e}")
            return []

        clients = []
        for platform in platforms:
            if isinstance(platform, AiocqhttpAdapter):
                try:
                    client = platform.get_client()
                    if client:
                        clients.append(client)
                except Exception as e:
                    logger.warning(f"获取平台实例 client 失败: {e}")
        return clients

    async def _onebot_send_group(self, group_id: str, message: str, clients: list):
        if not clients:
            logger.warning(f"未找到可用的 aiocqhttp 平台实例，无法发送至群 {group_id}")
            return

        for client in clients:
            try:
                gid = int(group_id)
                await client.api.call_action(
                    "send_group_msg", group_id=gid, message=message
                )
            except Exception as e:
                logger.warning(f"发送至群 {group_id} 失败: {e}")

    async def _onebot_send_private(self, user_id: str, message: str, clients: list):
        if not clients:
            logger.warning(f"未找到可用的 aiocqhttp 平台实例，无法发送至私聊 {user_id}")
            return

        for client in clients:
            try:
                uid = int(user_id)
                await client.api.call_action(
                    "send_private_msg", user_id=uid, message=message
                )
            except Exception as e:
                logger.warning(f"发送至私聊 {user_id} 失败: {e}")

    async def _send_to_targets(self, message: str):
        target_groups = self.config.get("target_groups", []) or []
        target_users = self.config.get("target_users", []) or []
        target_sessions = self.config.get("target_sessions", []) or []

        if not target_groups and not target_users and not target_sessions:
            logger.warning("没有配置任何推送目标，跳过发送")
            return

        clients = self._get_aiocqhttp_clients()

        for gid in target_groups:
            await self._onebot_send_group(str(gid), message, clients)
        for uid in target_users:
            await self._onebot_send_private(str(uid), message, clients)

        for umo in target_sessions:
            try:
                chain = MessageChain().message(message)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"发送至 {umo} 失败: {e}")

    @filter.command("ds")
    async def query_status(self, event: AstrMessageEvent):
        """查询当前 DeepSeek 峰谷计价状态"""
        now = datetime.now(self._get_tz())
        status, _ = self._get_status(now)
        if status == "peak":
            reply = self._pick_or_default("ds_peak_message", DS_DEFAULT_PEAK)
        elif self._weekend_valley_enabled() and self._is_weekend(now):
            reply = self._pick_or_default("ds_weekend_message", DS_DEFAULT_WEEKEND)
        else:
            reply = self._pick_or_default("ds_valley_message", DS_DEFAULT_VALLEY)
        yield event.plain_result(reply)

    @filter.command("dsid")
    async def get_session_id(self, event: AstrMessageEvent):
        """获取当前会话的 ID 信息，用于配置推送目标"""
        umo = event.unified_msg_origin
        platform_name = event.get_platform_name()

        lines = [
            "📋 当前会话信息",
            f"平台：{platform_name}",
            f"unified_msg_origin：{umo}",
        ]

        if platform_name == ONE_BOT_PLATFORM:
            group_id = ""
            try:
                group_id = str(event.get_group_id() or "")
            except Exception:
                group_id = ""

            if group_id:
                lines.append(f"群号：{group_id}")
                lines.append(f'→ 填入配置项 target_groups：["{group_id}"]')
            else:
                sender_id = str(event.get_sender_id())
                lines.append(f"QQ 号：{sender_id}")
                lines.append(f'→ 填入配置项 target_users：["{sender_id}"]')

        lines.append("")
        lines.append("💡 其他平台的完整 umo 请填入 target_sessions 配置项")
        lines.append("💡 也可使用 /sid 命令获取完整 unified_msg_origin")

        yield event.plain_result("\n".join(lines))
