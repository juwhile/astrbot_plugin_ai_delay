import asyncio
import os
import random
import re
import time
import urllib.request

from astrbot.api import logger, AstrBotConfig
from astrbot.api import message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 匹配模型复述的工具调用文本，如 [调用工具 image-analysis: {...}]
_TOOL_ECHO_RE = re.compile(r"\[调用工具[^\]]*\]", re.DOTALL)

# 图片/文件组件类型（需保存到本地以防 URL 过期）
_IMAGE_COMPONENT_TYPES = {"Image", "File", "Record", "Video"}
# 图片缓存保留时长（秒），1 分钟后自动删除
_IMG_TTL = 60  # 秒


def _get_or_new_loop():
    """获取当前或新建一个事件循环（供后台延迟删除用）。"""
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _write_file(path: str, data: bytes) -> None:
    """把 bytes 写入文件（在 asyncio.to_thread 中执行，避免阻塞事件循环）。"""
    with open(path, "wb") as f:
        f.write(data)


@register("astrbot_plugin_ai_delay", "uwhile", "AI回复随机延迟，白名单ID豁免", "1.2.2")
class AiDelayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_msg_time = {}  # 会话ID -> 最后一条消息时间戳（消息合并用）
        self._msg_buffer = {}  # 会话ID -> [被合并消息的文本列表]
        self._saved_img_paths = {}  # 会话ID -> [本地已保存的图片路径（合并后追加给AI）]

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
        """图片收集开关（独立于群聊延迟/合并总开关）。

        即使 group_switch=False（群聊不延迟不合并），只要本开关开启，
        仍会在收到群聊图片时下载到本地缓存，防止 URL 过期导致 AI 识别不到。
        由 _conf_schema.json 的 image_collect 定义注入到 self.config。
        """
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
        """免合并名单：名单内的用户不做合并等待，直接走原回复流程"""
        exempt = self.config.get("merge_exempt_ids", [])
        if isinstance(exempt, str):
            exempt = [exempt]
        return str(event.get_sender_id()) in {str(x).strip() for x in exempt}

    def _is_group(self, event: AstrMessageEvent) -> bool:
        """判断是否为群聊消息。官方文档：AstrBotMessage.group_id 非空即群聊，私聊为空。"""
        try:
            return bool(getattr(event.message_obj, "group_id", ""))
        except Exception:
            return False

    def _session_key(self, event: AstrMessageEvent) -> str:
        """统一的会话 key：群聊用 群ID:发送者ID，私聊用 发送者ID。
        图片消息与随后的文本消息必须命中同一 key，才能跨消息挂载图片。"""
        try:
            gid = getattr(event.message_obj, "group_id", "") or ""
        except Exception:
            gid = ""
        sid = str(event.get_sender_id())
        return f"{gid}:{sid}" if gid else sid

    def _group_enabled(self) -> bool:
        """群聊延迟与合并总开关。关闭时群聊消息不延迟、不合并，仅私聊生效。"""
        try:
            return bool(self.config.get("group_switch", True))
        except Exception:
            return True

    # 视为"有实际内容"的消息组件类型
    _CONTENT_TYPES = {"Plain", "Image", "Record", "Video", "File", "Json", "Xml", "Reply", "At"}

    async def _save_media_to_cache(self, event: AstrMessageEvent) -> None:
        """把消息里的图片/文件组件下载到本地缓存，1分钟后自动删除。

        QQ 图片 URL 会过期，AI 识别前可能已 403 失效。收到含图片的
        消息时立即下载到本地文件，并替换组件 URL 为本地路径，确保
        AI 调用 image-analysis 时读取的是不过期的本地文件。

        使用 asyncio.to_thread() 把同步阻塞的下载/写盘移出事件循环，
        避免含图片消息卡住整个 AstrBot 的消息处理（审查反馈点1）。
        """
        # 图片收集只对群聊生效：私聊图片 URL 一般不常过期，无需缓存，
        # 且私聊消息不应被图片缓存/挂载逻辑干扰。
        if not self._is_group(event):
            return
        try:
            # 收集图片/文件 URL（来自组件）
            urls = []
            for comp in (event.message_obj.message or []):
                ctype = str(getattr(comp, "type", "") or type(comp).__name__).split(".")[-1]
                logger.info(f"[图片缓存] 组件 type={ctype} "
                            f"url={getattr(comp,'url',None)!r} file={getattr(comp,'file',None)!r} "
                            f"path={getattr(comp,'path',None)!r} dict={getattr(comp,'__dict__',{})!r}")
                if ctype not in _IMAGE_COMPONENT_TYPES:
                    continue
                # 组件属性：image.url / file.url / record.url / video.url / url
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

            # 确保保存目录存在（AstrBot 插件数据目录，审查反馈点2）
            # __file__ = AstrBot/data/plugins/astrbot_plugin_ai_delay/main.py
            # 上三级才是 AstrBot 根目录
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            save_dir = os.path.join(base, "data", "plugin_data", "astrbot_plugin_ai_delay", "img_cache")
            os.makedirs(save_dir, exist_ok=True)

            saved_paths = []
            for ctype, url in urls:
                try:
                    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
                    fname = f"{int(time.time()*1000)}_{abs(hash(url))%10**8}{ext}"
                    path = os.path.join(save_dir, fname)

                    # 同步下载/写盘放到线程池执行，不阻塞事件循环
                    def _download():
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.read()

                    data = await asyncio.to_thread(_download)
                    await asyncio.to_thread(_write_file, path, data)
                    saved_paths.append((ctype, path, url))
                    logger.info(f"[图片缓存] 已保存 {ctype} -> {path} ({len(data)}B)")
                    # 1分钟后删除（异步线程安全）
                    self._schedule_del(path)
                except Exception as e:
                    logger.warning(f"[图片缓存] 下载失败 {ctype} {url[:60]}: {e}")

            # 把组件 URL 替换为本地路径，让 AI 读本地文件
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
                        # 也更新 Image 组件的 file 字段（若存在）
                        if ctype == "Image" and hasattr(comp, "file"):
                            comp.file = new_path
                # 记录到会话缓存，合并时追加为 Image 组件（覆盖"图片在前一条"的情况）
                session = self._session_key(event)
                for ctype, path, url in saved_paths:
                    self._saved_img_paths.setdefault(session, []).append(path)
        except Exception as e:
            logger.warning(f"[图片缓存] 处理失败: {e}")

    def _schedule_del(self, path: str) -> None:
        """1 分钟后删除缓存文件。优先用当前事件循环的 call_later，
        若不在事件循环中则用 asyncio 新任务（线程安全）。"""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # 无事件循环：起一个临时循环执行延迟删除
            async def _del_later():
                await asyncio.sleep(_IMG_TTL)
                self._del_after(path)
            asyncio.run_coroutine_threadsafe(_del_later(), _get_or_new_loop())
            return
        loop.call_later(_IMG_TTL, lambda: self._del_after(path))

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
        logger.info(f"[AI延迟] merge_burst_messages 收到消息, group={self._is_group(event)}, "
                    f"str={getattr(event, 'message_str', '')[:30]!r}, "
                    f"comps={[str(getattr(c,'type','') or type(c).__name__).split('.')[-1] for c in (getattr(getattr(event,'message_obj',None),'message',None) or [])]}")
        if window <= 0:
            return
        # 图片收集独立开关：只要开启就下载图片到本地（防 URL 过期），
        # 不受下方群聊延迟/合并总开关 group_switch 影响
        _ic = self._image_collect_enabled()
        logger.info(f"[图片缓存] image_collect_enabled={_ic}, "
                    f"config_keys={list(self.config.keys()) if hasattr(self.config, 'keys') else 'n/a'}")
        if _ic:
            try:
                await self._save_media_to_cache(event)
            except Exception as e:
                logger.warning(f"[AI延迟] 保存媒体到缓存失败: {e}")
        # 群聊延迟与合并开关：关闭时群聊消息不参与合并，直接放行。
        # 但已保存的本地图片要挂到当前事件上，让 AI 的 image-analysis 能读到。
        if self._is_group(event) and not self._group_enabled():
            if self._image_collect_enabled():
                try:
                    # 图片消息自身无文本，AI 不处理；图片必须挂到"下一条有文本的消息"上。
                    # 所以这里不立即 pop/挂载，而是把图片路径暂存到 pending，
                    # 等待下一条文本消息（如 @uwhile 看图片）到来时再追加。
                    # 此时消息若有文本（如 "图片 看图片"），直接挂到当前消息。
                    text = (event.message_str or "").strip()
                    msg_list = getattr(event.message_obj, "message", None) or []
                    has_img = any(str(getattr(c, "type", "") or type(c).__name__).split(".")[-1]
                                   == "Image" for c in msg_list)
                    if text and not has_img:
                        # 当前消息有文本且本身无图片：把之前保存的图片挂到当前消息上
                        session = self._session_key(event)
                        saved = self._saved_img_paths.pop(session, [])
                        if saved:
                            for p in saved:
                                msg_list.append(Comp.Image(file=p, url=p))
                            event.message_obj.message = msg_list
                            logger.info(f"[AI延迟] 群聊开关关闭，已挂载 {len(saved)} 张本地图片到本条消息供 AI 识别")
                        else:
                            logger.info(f"[AI延迟] 群聊开关关闭，文本消息无待挂载图片（session={session}），"
                                        f"当前消息自身 Image 数={sum(1 for c in msg_list if str(getattr(c,'type','') or type(c).__name__).split('.')[-1]=='Image')}")
                except Exception as e:
                    logger.warning(f"[AI延迟] 挂载本地图片失败: {e}")
            return
        # 指令不合并（避免影响 /延迟测试 等命令）
        if event.message_str.startswith("/"):
            return
        # 免合并名单：名单内的用户不合并、不等待
        if self._merge_exempt(event):
            return
        text = (event.message_str or "").strip()
        # 先保存图片/文件到本地缓存（防 URL 过期），再判断是否空壳消息。
        # 这样 AI 调用 image-analysis 时读到的是本地文件，不会 403。
        try:
            await self._save_media_to_cache(event)
        except Exception as e:
            logger.warning(f"[AI延迟] 保存媒体到缓存失败: {e}")
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
        session = self._session_key(event)
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
            # 追加本会话已保存的本地图片组件，确保 AI 的 image-analysis 能读到
            # （覆盖"图片在连发消息中的非最后一条"或"纯图片无文字"的情况）
            saved = self._saved_img_paths.pop(session, [])
            if saved:
                try:
                    msg_list = getattr(event.message_obj, "message", None) or []
                    for p in saved:
                        msg_list.append(Comp.Image(file=p, url=p))
                    event.message_obj.message = msg_list
                    logger.info(f"[AI延迟] 已为合并消息追加 {len(saved)} 张本地图片供 AI 识别")
                except Exception as e:
                    logger.warning(f"[AI延迟] 追加本地图片失败: {e}")
            logger.info(f"[AI延迟] 已合并 {len(buf)} 条连发消息一起请求 AI")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp):
        """LLM 请求完成后、消息发送前执行，阻塞在此处以实现延迟。"""
        if self._is_exempt(event):
            return
        # 群聊延迟与合并开关：关闭时群聊消息不延迟
        if self._is_group(event) and not self._group_enabled():
            logger.info("[AI延迟] 群聊开关已关闭，跳过群聊回复延迟")
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
