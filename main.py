import asyncio
import random
import re

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 匹配模型复述的工具调用文本，如 [调用工具 image-analysis: {...}]
_TOOL_ECHO_RE = re.compile(r"\[调用工具[^\]]*\]", re.DOTALL)


@register("astrbot_plugin_ai_delay", "uwhile", "AI回复随机延迟，白名单ID豁免", "1.1.0")
class AiDelayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

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
