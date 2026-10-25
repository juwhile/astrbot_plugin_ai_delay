import asyncio
import random
import re
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 匹配模型复述的工具调用文本，如 [调用工具 image-analysis: {...}]
_TOOL_ECHO_RE = re.compile(r"\[调用工具[^\]]*\]", re.DOTALL)


@register("astrbot_plugin_ai_delay", "uwhile", "AI回复随机延迟，白名单ID豁免", "1.2.0")
class AiDelayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_msg_time = {}  # 会话ID -> 最后一条消息时间戳（消息合并用）
        self._msg_buffer = {}  # 会话ID -> [被合并消息的文本列表]

    def _get_delay_range(self):
        try:
            tmin = float(self.config.get("min_delay", 1))
            tmax = float(self.config.get("max_delay", 5))
        except (TypeError, ValueError):
            tmin, tmax = 1.0, 5.0
        if tmax < tmin:
            tmin, tmax = tmax, tmin
        return tmin, tmax

    def _is_exempt(self, event: AstrMessageEvent) -> bool:
        exempt = self.config.get("exempt_ids", [])
        if isinstance(exempt, str):
            exempt = [exempt]
        return str(event.get_sender_id()) in {str(x).strip() for x in exempt}

    def _merge_exempt(self, event: AstrMessageEvent) -> bool:
        """免合并名单：名单内的用户不做合并等待，直接走原回复流程"""
        exempt = self.config.get("merge_exempt_ids", [])
        if isinstance(exempt, str):
            exempt = [exempt]
        return str(event.get_sender_id()) in {str(x).strip() for x in exempt}

    # 视为"有实际内容"的消息组件类型
    _CONTENT_TYPES = {"Plain", "Image", "Record", "Video", "File", "Json", "Xml", "Reply", "At"}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def merge_burst_messages(self, event: AstrMessageEvent):
        """消息合并窗口：连发多条消息时，把内容合并后只回复最后一条"""
        try:
            window = float(self.config.get("merge_window", 5))
        except (TypeError, ValueError):
            window = 5.0
        if window <= 0:
            return
        # 指令不合并（避免影响 /延迟测试 等命令）
        if event.message_str.startswith("/"):
            return
        # 免合并名单：名单内的用户不合并、不等待
        if self._merge_exempt(event):
            return
        text = (event.message_str or "").strip()
        # 无文本且无内容组件的事件（对方正在输入、心跳等空壳）直接忽略，
        # 不进缓冲、不重置窗口、不触发回复
        if not text:
            has_content = False
            for comp in (event.message_obj.message or []):
                # 枚举 str() 结果形如 "ComponentType.Image"，取最后一段归一化
                ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                if ctype in self._CONTENT_TYPES:
                    has_content = True
                    break
            if not has_content:
                event.stop_event()
                return
        session = event.unified_msg_origin
        now = time.monotonic()
        self._last_msg_time[session] = now
        # 本条进入合并缓冲区：有文本用文本，纯图片/语音等用对应占位符
        if text:
            self._msg_buffer.setdefault(session, []).append(text)
        else:
            placeholder = "[图片]"
            for comp in (event.message_obj.message or []):
                ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                if ctype == "Record":
                    placeholder = "[语音]"
                    break
                if ctype == "Video":
                    placeholder = "[视频]"
                    break
            self._msg_buffer.setdefault(session, []).append(placeholder)
        await asyncio.sleep(window)
        if self._last_msg_time.get(session) != now:
            # 后面还有新消息：本条交给最新那条的请求处理
            logger.info("[AI延迟] 会话在合并窗口内收到新消息，本条文本并入下一条请求")
            event.stop_event()
            return
        # 本条是最后一条：把缓冲区里合并的所有消息一起发给 AI
        buf = self._msg_buffer.get(session) or []
        self._msg_buffer[session] = []
        if buf:
            combined = "\n".join(f"[连发消息{i+1}] {t}" for i, t in enumerate(buf))
            try:
                event.message_str = combined
                event.message_obj.message_str = combined
            except Exception:
                pass
            logger.info(f"[AI延迟] 已合并 {len(buf)} 条连发消息一起请求 AI")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp):
        """LLM 请求完成后、消息发送前执行，阻塞在此处以实现延迟。"""
        if self._is_exempt(event):
            return
        tmin, tmax = self._get_delay_range()
        delay = random.uniform(tmin, tmax)
        logger.info(f"[AI延迟] 用户 {event.get_sender_name()} 不在白名单，延迟 {delay:.1f} 秒后发送回复")
        await asyncio.sleep(delay)

    @filter.command("延迟测试", alias={"delaytest"})
    async def delay_test(self, event: AstrMessageEvent):
        """临时测试：模拟一次随机延迟回复，任何人可用"""
        tmin, tmax = self._get_delay_range()
        delay = random.uniform(tmin, tmax)
        exempt = "在白名单" if self._is_exempt(event) else "不在白名单"
        yield event.plain_result(f"开始模拟：本次将延迟 {delay:.1f} 秒...")
        await asyncio.sleep(delay)
        yield event.plain_result(f"✅ 延迟 {delay:.1f} 秒结束，消息发送成功（当前用户{exempt}，{event.get_sender_id()}）")

    @filter.on_decorating_result()
    async def strip_tool_echo(self, event: AstrMessageEvent):
        """发送前清理模型复述的工具调用文本"""
        result = event.get_result()
        if not result or not result.chain:
            return
        for comp in result.chain:
            if hasattr(comp, "text") and comp.text and "[调用工具" in comp.text:
                cleaned = _TOOL_ECHO_RE.sub("", comp.text).strip()
                if cleaned != comp.text:
                    logger.info("[AI延迟] 已过滤模型复述的工具调用文本")
                comp.text = cleaned
