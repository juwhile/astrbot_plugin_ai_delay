import asyncio
import os
import random
import re
import time
import urllib.request
import threading
from urllib.parse import urlparse

from astrbot.api import logger, AstrBotConfig
from astrbot.api import message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 匹配模型复述的工具调用文本，如 [调用工具 image-analysis: {...}]
_TOOL_ECHO_RE = re.compile(r"\[调用工具[^\]]*\]", re.DOTALL)

# 图片组件类型（只缓存真正的图片，不缓存 APK/ZIP 等普通文件，避免模型识别报错）
_IMAGE_COMPONENT_TYPES = {"Image"}
# 图片缓存保留时长（秒）
_IMG_TTL = 60


def _write_file(path: str, data: bytes) -> None:
    """把 bytes 写入文件"""
    with open(path, "wb") as f:
        f.write(data)


@register("astrbot_plugin_ai_delay", "uwhile", "AI回复随机延迟，白名单ID豁免", "1.2.2")
class AiDelayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_msg_time = {}  # 会话ID -> 最后一条消息时间戳
        self._msg_buffer = {}  # 会话ID -> [被合并消息的文本列表]
        self._saved_img_paths = {}  # 会话ID -> [本地已保存的图片路径]
        
        # 启动后台内存清理任务
        asyncio.create_task(self._cleanup_memory())

    async def _cleanup_memory(self):
        """每小时清理一次超过1小时不活跃的会话缓存，防止内存泄漏"""
        while True:
            await asyncio.sleep(3600)
            now = time.monotonic()
            for session, last_time in list(self._last_msg_time.items()):
                if now - last_time > 3600:
                    self._last_msg_time.pop(session, None)
                    self._msg_buffer.pop(session, None)
                    self._saved_img_paths.pop(session, None)

    def _get_delay_range(self):
        try:
            tmin = float(self.config.get("min_delay", 1))
            tmax = float(self.config.get("max_delay", 5))
        except (TypeError, ValueError):
            tmin, tmax = 1.0, 5.0
        if tmax < tmin:
            tmin, tmax = tmax, tmin
        return tmin, tmax

    def _image_collect_enabled(self) -> bool:
        """图片收集开关（独立于群聊延迟/合并总开关）。"""
        try:
            return bool(self.config.get("image_collect", True))
        except Exception:
            return True

    def _is_exempt(self, event: AstrMessageEvent) -> bool:
        exempt = self.config.get("exempt_ids", [])
        if isinstance(exempt, str):
            exempt = [exempt]
        return str(event.get_sender_id()) in {str(x).strip() for x in exempt}

    def _merge_exempt(self, event: AstrMessageEvent) -> bool:
        exempt = self.config.get("merge_exempt_ids", [])
        if isinstance(exempt, str):
            exempt = [exempt]
        return str(event.get_sender_id()) in {str(x).strip() for x in exempt}

    def _is_group(self, event: AstrMessageEvent) -> bool:
        """判断是否为群聊消息。"""
        try:
            return bool(getattr(event.message_obj, "group_id", ""))
        except Exception:
            return False

    def _session_key(self, event: AstrMessageEvent) -> str:
        """统一的会话 key：群聊用 群ID:发送者ID，私聊用 发送者ID。"""
        try:
            gid = getattr(event.message_obj, "group_id", "") or ""
        except Exception:
            gid = ""
        sid = str(event.get_sender_id())
        return f"{gid}:{sid}" if gid else sid

    def _group_enabled(self) -> bool:
        """群聊延迟与合并总开关。"""
        try:
            return bool(self.config.get("group_switch", True))
        except Exception:
            return True

    _CONTENT_TYPES = {"Plain", "Image", "Record", "Video", "File", "Json", "Xml", "Reply", "At"}

    async def _save_media_to_cache(self, event: AstrMessageEvent) -> None:
        """把群聊图片下载到本地缓存，替换组件URL，1分钟后自动删除。"""
        if not self._is_group(event):
            return
        try:
            urls = []
            for comp in (event.message_obj.message or []):
                ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                if ctype not in _IMAGE_COMPONENT_TYPES:
                    continue
                url = None
                for attr in ("url", "file_url", "path"):
                    v = getattr(comp, attr, None)
                    if v:
                        url = v
                        break
                if url and str(url).startswith(("http://", "https://")):
                    urls.append((ctype, url))
            if not urls:
                return

            # 修正了这里的路径，去掉了多余的 data/data
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            save_dir = os.path.join(base, "data", "plugin_data", "astrbot_plugin_ai_delay", "img_cache")
            os.makedirs(save_dir, exist_ok=True)

            saved_paths = []
            for ctype, url in urls:
                try:
                    # 修正后缀，防止奇怪的URL导致图片无法被识别
                    parsed_url = urlparse(url)
                    ext = os.path.splitext(parsed_url.path)[1].lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                        
                    fname = f"{int(time.time()*1000)}_{abs(hash(url))%10**8}{ext}"
                    path = os.path.join(save_dir, fname)

                    def _download():
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.read()

                    data = await asyncio.to_thread(_download)
                    # 文件写入很快，不需要 to_thread，减少线程切换开销
                    _write_file(path, data)
                    
                    saved_paths.append((ctype, path, url))
                    logger.info(f"[图片缓存] 已保存 {ctype} -> {path} ({len(data)}B)")
                    self._schedule_del(path)
                except Exception as e:
                    logger.warning(f"[图片缓存] 下载失败 {ctype} {url[:60]}: {e}")

            if saved_paths:
                saved_by_type = {}
                for ctype, path, url in saved_paths:
                    saved_by_type.setdefault(ctype, []).append((url, path))
                for comp in (event.message_obj.message or []):
                    ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                    if ctype in saved_by_type:
                        pairs = saved_by_type.get(ctype)
                        if not pairs:
                            continue
                        old_url, new_path = pairs.pop(0)
                        for attr in ("url", "file_url", "path"):
                            if getattr(comp, attr, None) == old_url:
                                setattr(comp, attr, new_path)
                                break
                        if ctype == "Image" and hasattr(comp, "file"):
                            comp.file = new_path
                
                session = self._session_key(event)
                for ctype, path, url in saved_paths:
                    self._saved_img_paths.setdefault(session, []).append(path)
        except Exception as e:
            logger.warning(f"[图片缓存] 处理失败: {e}")

    def _schedule_del(self, path: str) -> None:
        """1 分钟后删除缓存文件。"""
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(_IMG_TTL, lambda: self._del_after(path))
        except RuntimeError:
            # 如果在非异步上下文，起一个守护线程延迟删除
            def _del_later():
                time.sleep(_IMG_TTL)
                self._del_after(path)
            threading.Thread(target=_del_later, daemon=True).start()

    def _del_after(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"[图片缓存] 已删除过期缓存 {path}")
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def merge_burst_messages(self, event: AstrMessageEvent):
        """消息合并窗口：连发多条消息时，把内容合并后只回复最后一条"""
        try:
            window = float(self.config.get("merge_window", 5))
        except (TypeError, ValueError):
            window = 5.0
        
        if window <= 0:
            return
            
        if self._image_collect_enabled():
            try:
                await self._save_media_to_cache(event)
            except Exception as e:
                logger.warning(f"[AI延迟] 保存媒体到缓存失败: {e}")
                
        if self._is_group(event) and not self._group_enabled():
            if self._image_collect_enabled():
                try:
                    text = (event.message_str or "").strip()
                    msg_list = getattr(event.message_obj, "message", None) or []
                    has_img = any(str(getattr(c, "type", "") or type(c).__name__).split(".")[-1] == "Image" for c in msg_list)
                    if text and not has_img:
                        session = self._session_key(event)
                        saved = self._saved_img_paths.pop(session, [])
                        if saved:
                            for p in saved:
                                msg_list.append(Comp.Image(file=p, url=p))
                            event.message_obj.message = msg_list
                            logger.info(f"[AI延迟] 群聊开关关闭，已挂载 {len(saved)} 张本地图片")
                except Exception as e:
                    logger.warning(f"[AI延迟] 挂载本地图片失败: {e}")
            return
            
        if event.message_str.startswith("/"):
            return
        if self._merge_exempt(event):
            return
            
        text = (event.message_str or "").strip()
        try:
            await self._save_media_to_cache(event)
        except Exception as e:
            logger.warning(f"[AI延迟] 保存媒体到缓存失败: {e}")
            
        if not text:
            has_content = False
            for comp in (event.message_obj.message or []):
                ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                if ctype in self._CONTENT_TYPES:
                    has_content = True
                    break
            if not has_content:
                event.stop_event()
                return
                
        session = self._session_key(event)
        now = time.monotonic()
        self._last_msg_time[session] = now
        
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
            
        # 阻塞等待合并窗口
        await asyncio.sleep(window)
        
        # 发现后面还有新消息，当前这条直接被丢弃，交由最后一条处理
        if self._last_msg_time.get(session) != now:
            event.stop_event()
            return
            
        # 这是最后一条消息，提取缓冲区内容并清理
        buf = self._msg_buffer.get(session) or []
        self._msg_buffer[session] = []
        
        if buf:
            combined = "\n".join(f"[连发消息{i+1}] {t}" for i, t in enumerate(buf))
            try:
                event.message_str = combined
                event.message_obj.message_str = combined
            except Exception:
                pass
                
            saved = self._saved_img_paths.pop(session, [])
            if saved:
                try:
                    msg_list = getattr(event.message_obj, "message", None) or []
                    for p in saved:
                        msg_list.append(Comp.Image(file=p, url=p))
                    event.message_obj.message = msg_list
                    logger.info(f"[AI延迟] 已为合并消息追加 {len(saved)} 张本地图片")
                except Exception as e:
                    logger.warning(f"[AI延迟] 追加本地图片失败: {e}")
            logger.info(f"[AI延迟] 已合并 {len(buf)} 条连发消息一起请求 AI")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp):
        """LLM 请求完成后、消息发送前执行，阻塞在此处以实现延迟。"""
        if self._is_exempt(event):
            return
        if self._is_group(event) and not self._group_enabled():
            return
            
        tmin, tmax = self._get_delay_range()
        delay = random.uniform(tmin, tmax)
        logger.info(f"[AI延迟] 用户 {event.get_sender_name()} 延迟 {delay:.1f} 秒后发送")
        await asyncio.sleep(delay)

    @filter.command("延迟测试", alias={"delaytest"})
    async def delay_test(self, event: AstrMessageEvent):
        """临时测试：模拟一次随机延迟回复，任何人可用"""
        tmin, tmax = self._get_delay_range()
        delay = random.uniform(tmin, tmax)
        exempt = "在白名单" if self._is_exempt(event) else "不在白名单"
        yield event.plain_result(f"开始模拟：本次将延迟 {delay:.1f} 秒...")
        await asyncio.sleep(delay)
        yield event.plain_result(f"✅ 延迟 {delay:.1f} 秒结束（当前用户{exempt}，{event.get_sender_id()}）")

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