"""B 站视频侦察兵：让 bot 主动刷 B 站，把视频卡片/视频文件转到 QQ。

指令
----
/刷视频                从兴趣关键词池里随机找一个没发过的视频
/找视频 <关键词>        按关键词搜一个；/找视频 猫 3 取第 3 条
/视频订阅开             当前会话加入定时推送
/视频订阅关             当前会话退出定时推送
/视频订阅状态           查看订阅与运行状态
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import importlib.util

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

# AstrBot 用 spec_from_file_location 加载 main.py，包上下文不成立，
# 相对导入会炸。这里直接从同目录按文件路径加载，绕开 sys.path 差异。
_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_bili_scout_api", Path(__file__).parent / "bili_api.py"
)
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)  # type: ignore[union-attr]
BiliClient, BiliError = _api.BiliClient, _api.BiliError

try:  # v4
    from astrbot.core.message.message_event_result import MessageChain
except ImportError:  # v3 兼容
    from astrbot.api.event import MessageChain  # type: ignore

try:
    from astrbot.api.message_components import Video
except ImportError:  # 老版本没有 Video 组件，视频模式自动降级
    Video = None  # type: ignore

try:  # 结构化卡片（QQ 的分享卡片/小程序卡片都走这个）
    from astrbot.api.message_components import Json as JsonBase
except ImportError:
    JsonBase = None  # type: ignore

_llm_tool = getattr(filter, "llm_tool", None)
if _llm_tool is None:  # 老版本没有 llm_tool，退化成普通方法（不可被 LLM 调用）
    def _llm_tool(name):  # type: ignore[misc]
        def _deco(fn):
            return fn
        return _deco


PLUGIN_NAME = "astrbot_plugin_bili_scout"
PLUGIN_VER = "1.7.0"
_LEGACY_DIR = Path(__file__).parent / "data"
_DATA_DIR: Path | None = None
_SUBS_FILE = "subscriptions.json"
_HISTORY_FILE = "history.json"
_PUSH_LOG_FILE = "push_log.json"
HISTORY_LIMIT = 600
PUSH_RECENT_LIMIT = 60


def _data_dir() -> Path:
    """插件数据目录。

    优先用 AstrBot 的 `data/plugin_data/<插件名>`——升级/重装插件不会丢；
    拿不到（老版本或 StarTools 还没初始化）就退回包内 `data/`。
    首次切过去时把老目录里的 json 搬一份，别让已订阅的会话凭空消失。
    """
    global _DATA_DIR
    if _DATA_DIR is not None:
        return _DATA_DIR
    target: Path | None = None
    try:
        from astrbot.api.star import StarTools
        target = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001 - 拿不到就用包内目录，功能照跑
        target = None
    if target is None:
        target = _LEGACY_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = _LEGACY_DIR
        target.mkdir(parents=True, exist_ok=True)
    if target != _LEGACY_DIR and _LEGACY_DIR.is_dir():
        for old in _LEGACY_DIR.glob("*.json"):
            new = target / old.name
            if not new.exists():
                try:
                    shutil.copy2(old, new)
                except OSError:
                    pass
    _DATA_DIR = target
    return target

# 自然语言触发：不要求打斜杠命令，随口说“刷几个视频发我看看”就行。
# 第三条是给“去B站搬点XX过来”这种说的——只要带上 B 站，就别管他有没有说“视频”
# 这两个字。第四条是“刷点B站的”这种动词在前的说法。
_NL_SCOUT_RE = (
    r"(?i)(?:刷|找|搜|捞|整|挑|推荐|搬|拿|发|来).{0,10}(?:视频|片子|短片)"
    r"|(?:视频|片子).{0,8}(?:推荐|来一个|发一个|发我|看看|几个)"
    r"|(?:b站|哔哩|BILI).{0,12}(?:刷|找|搜|捞|整|挑|推荐|搬|拿|发|来)"
    r"|(?:刷|找|搜|捞|整|挑|推荐|搬|拿|发|来).{0,8}(?:b站|哔哩)"
    r"|(?:刷|找|搜|捞|整|挑|推荐|搬|拿|发|来)(?:点|些|几个|一点|一些)(?:.{1,12})?(?:过来|给我|来看看|瞅瞅)"
)

# 文本里没提到已配置关键词时，从这些常见标签里兜一个
_COMMON_TAGS = (
    "搞笑", "沙雕", "鬼畜", "猫", "狗", "萌宠", "美食", "科技", "游戏",
    "音乐", "舞蹈", "动漫", "鬼故事", "纪录片", "知识", "整活",
)

# 从“找点三国杀的视频”“去B站搬点屎过来”这类句子里把关键词抠出来。
# 结尾的“视频/片子”可有可无——带 B 站的说法往往根本不提这两字。
_WANT_RE = re.compile(
    r"(?:刷|找|搜|捞|整|挑|推荐|搬|拿|来|发)"
    r"(?:点|些|几个|一点|一个|一下)?\s*"
    r"([^\s，。,.!！?？、@bB站]{1,15}?)\s*"
    r"(?:过来|给我|来看看|看看|的视频|视频|片子|短片|吧)"
)

# 抠出来是这种空话就当没抠到
_VAGUE_RE = re.compile(
    r"^(有趣|搞笑|好玩|好看|优质|热门|精彩|可爱|沙雕|不错|什么|随便|一些|几[个条]?)"
)



def _at_me(event: AstrMessageEvent) -> bool:
    """消息链里是否真的 @ 了我。群聊里只认这个，比唤醒标志靠谱。"""
    try:
        me = str(event.get_self_id())
        for comp in event.get_messages():
            t = getattr(comp, "type", "")
            if getattr(t, "name", t) == "At" and str(getattr(comp, "qq", "")) == me:
                return True
    except Exception:  # noqa: BLE001 - 平台适配器五花八门，判不出来就当没 @
        return False
    return False

# 没有可用 LLM 时兜底的观后感（不依赖人设，中性口语）
FALLBACK_COMMENTS = [
    "刚看完，还挺上头的。",
    "看完了，确实有点东西。",
    "这个我居然真看完了，笑了一下。",
    "看完缓了两秒，行吧。",
    "没什么好说的，就是好看。",
]


def _fmt_ts(ts: float | None, with_date: bool = True) -> str:
    """把时间戳写成「10-28 15:04」这种一眼能读的样子。"""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(float(ts))
    return dt.strftime("%m-%d %H:%M") if with_date else dt.strftime("%H:%M")


def _fmt_delta(seconds: float) -> str:
    """把「还有多久」写成「2 小时 15 分」这种。"""
    s = int(max(0, seconds))
    if s < 60:
        return "不到 1 分钟"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分钟"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} 小时" + (f" {m} 分" if m else "")
    d, h = divmod(h, 24)
    return f"{d} 天" + (f" {h} 小时" if h else "")


def _make_json_component(payload: dict):
    """把 dict 塞进 Json 组件，且保留成字符串。

    AstrBot 的 Json 组件会把 str 解析成 dict，最终序列化成
    {"type":"json","data":{"data":{...}}}；但 NapCat 要的是
    {"type":"json","data":{"data":"<json 字符串>"}}。
    所以构造后直接改实例字典，绕过 pydantic 的类型校验。
    """
    if JsonBase is None:
        return None
    comp = JsonBase(data={})
    object.__setattr__(comp, "data", json.dumps(payload, ensure_ascii=False))
    return comp


def _load_json(name: str, default):
    try:
        with open(_data_dir() / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(name: str, data) -> None:
    path = _data_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


@register(PLUGIN_NAME, "dsh", "让 bot 主动刷 B 站并把视频转发给你", PLUGIN_VER)
class BiliScout(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = BiliClient(
            sessdata=str(self._cfg("cookie_sessdata", "") or ""),
            bili_jct=str(self._cfg("cookie_bili_jct", "") or ""),
            timeout=int(self._cfg("request_timeout", 15) or 15),
        )
        self._warmed = False
        self._history: list[str] = list(_load_json(_HISTORY_FILE, []))
        self._subs: list[str] = list(_load_json(_SUBS_FILE, []))
        self._task: asyncio.Task | None = None
        self._comment_tasks: set[asyncio.Task] = set()
        self._last_push: dict[str, float] = {}
        self._platform_hint: str = ""
        # 推送历史：上次成功推到哪、什么时候；以及最近几条记录，用来对账
        log = _load_json(_PUSH_LOG_FILE, {})
        self._last_push_meta: dict[str, dict] = dict(log.get("last") or {})
        self._recent_pushes: list[dict] = list(log.get("recent") or [])[
            -PUSH_RECENT_LIMIT:
        ]
        # 内存里补一份时间戳，重启后间隔计算也接得上
        for umo, meta in self._last_push_meta.items():
            try:
                self._last_push[umo] = float(meta.get("ts") or 0)
            except (TypeError, ValueError):
                continue
        self._last_fail: dict[str, dict] = {}
        # 抓过的素材缓存 {bvid: (素材, 有没有字幕)}，推送循环会反复问同一批视频
        self._material_cache: dict[str, tuple[str, bool]] = {}  # 只存内存，失败原因不用长期留着
        self._bili_comment_ts: list[float] = []

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default=None):
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        try:  # AstrBotConfig 走 dict 协议，这里只是兜底
            return self.config[key]
        except Exception:  # noqa: BLE001
            return default

    @property
    def keywords(self) -> list[str]:
        raw = self._cfg("keywords", ["搞笑", "猫", "科技"])
        if isinstance(raw, str):
            raw = [s for s in re.split(r"[,，\s]+", raw) if s.strip()]
        return [str(k).strip() for k in (raw or []) if str(k).strip()]

    @property
    def auto_interval(self) -> int:
        try:
            return max(0, int(self._cfg("auto_interval_minutes", 120) or 0))
        except (TypeError, ValueError):
            return 0

    @property
    def push_mode(self) -> str:
        """interval = 隔一段时间推一次；fixed = 只在设定的几个时间点推。"""
        mode = str(self._cfg("push_mode", "interval") or "interval").strip().lower()
        return "fixed" if mode in ("fixed", "定点", "point") else "interval"

    def _push_times_list(self) -> list[tuple[int, int]]:
        """解析 push_times，认「9:00」「09:00」「21:30」这几种写法，认不出来的丢掉。"""
        raw = self._cfg("push_times", [])
        if isinstance(raw, str):
            raw = re.split(r"[,，\s]+", raw)
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[tuple[int, int]] = []
        for one in raw:
            m = re.match(r"^\s*(\d{1,2})\s*[:：]?\s*(\d{1,2})?\s*$", str(one or ""))
            if not m:
                continue
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59 and (hour, minute) not in out:
                out.append((hour, minute))
        return sorted(out)

    def _push_times_text(self) -> str:
        times = self._push_times_list()
        return "、".join(f"{h:02d}:{m:02d}" for h, m in times) if times else "（没设）"


    @property
    def auto_subscribe(self) -> bool:
        """用过一次就自动订阅。默认关——要推哪些会话，直接配 push_groups。"""
        raw = self._cfg("auto_subscribe", False)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(raw)

    @property
    def push_groups(self) -> list[str]:
        """要定时推送的会话。填群号即可，也接受完整 umo。"""
        raw = self._cfg("push_groups", [])
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,，\s]+", raw) if x.strip()]
        out: list[str] = []
        for x in raw or []:
            u = self._norm_umo(str(x))
            if u and u not in out:
                out.append(u)
        return out

    @property
    def push_hours(self) -> tuple[int, int] | None:
        """推送时段，返回当天分钟数区间；None 表示不限时段。

        认 `9-23`、`09:00-23:30`、`22-7` 这几种写法，跨午夜也支持。
        """
        raw = str(self._cfg("push_hours", "") or "").strip()
        if not raw:
            return None
        m = re.match(
            r"^(\d{1,2})(?::(\d{2}))?\s*[-~到]\s*(\d{1,2})(?::(\d{2}))?$", raw
        )
        if not m:
            return None
        h1, mi1 = int(m.group(1)), int(m.group(2) or 0)
        h2, mi2 = int(m.group(3)), int(m.group(4) or 0)
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and 0 <= mi1 < 60 and 0 <= mi2 < 60):
            return None
        start = min(h1 * 60 + mi1, 1440)
        end = min(h2 * 60 + mi2, 1440)
        if start == end:
            return None
        return (start, end)

    def _norm_umo(self, raw: str) -> str:
        """把「群号」补成完整 umo；已经是完整 umo 就原样返回。"""
        s = (raw or "").strip()
        if not s:
            return ""
        if ":" in s:
            return s
        plat = self._platform_hint or "aiocqhttp"
        return f"{plat}:GroupMessage:{s}"

    def _note_platform(self, event) -> None:
        """记下这个 bot 的平台标识，构造 umo 时用真名而不是硬编码。"""
        if self._platform_hint:
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        head = umo.split(":", 1)[0]
        if head:
            self._platform_hint = head

    def _target_umos(self) -> list[str]:
        """这一轮该推给谁：配置里写死的群 + 手动订阅的会话。"""
        targets = list(self.push_groups)
        for umo in self._subs:
            if umo not in targets:
                targets.append(umo)
        return targets

    def _in_push_window(self, now: float | None = None) -> bool:
        win = self.push_hours
        if win is None:
            return True
        lt = time.localtime(time.time() if now is None else now)
        cur = lt.tm_hour * 60 + lt.tm_min
        start, end = win
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # 跨午夜

    def _push_window_text(self) -> str:
        win = self.push_hours
        if win is None:
            return "不限时段"
        return f"{win[0] // 60:02d}:{win[0] % 60:02d} ~ {win[1] // 60:02d}:{win[1] % 60:02d}"

    @property
    def max_duration(self) -> int:
        try:
            return int(self._cfg("max_duration_seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def min_play(self) -> int:
        try:
            return int(self._cfg("min_play_count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def enable_video(self) -> bool:
        return bool(self._cfg("enable_video_file", False))

    @property
    def card_style(self) -> str:
        """card=QQ 分享卡片 / image=封面图+文字 / both=卡片+图"""
        style = str(self._cfg("card_style", "card") or "card").lower()
        return style if style in ("card", "image", "both") else "card"

    @property
    def card_type(self) -> str:
        """structmsg=QQ 分享链接卡片（稳）/ miniapp=小程序卡片（实验性）"""
        t = str(self._cfg("card_type", "structmsg") or "structmsg").lower()
        return t if t in ("structmsg", "miniapp") else "structmsg"

    @property
    def comment_enabled(self) -> bool:
        return bool(self._cfg("comment_enabled", True))

    @property
    def nl_trigger(self) -> bool:
        """是否允许“@我 刷几个视频看看”这种不带斜杠的自然语言触发。"""
        return bool(self._cfg("nl_trigger", True))

    @property
    def comment_read_content(self) -> bool:
        """写观后感之前，先去把这个视频的内容读一下（默认开）。"""
        return bool(self._cfg("comment_read_content", True))

    @property
    def comment_max_chars(self) -> int:
        try:
            return max(200, min(int(self._cfg("comment_max_chars", 2500) or 2500), 20000))
        except (TypeError, ValueError):
            return 2500

    @property
    def comment_retries(self) -> int:
        """抓字幕的重试次数。B 站字幕接口经常串号，多重试几次才稳。"""
        try:
            return max(2, min(int(self._cfg("comment_retries", 10) or 10), 20))
        except (TypeError, ValueError):
            return 10

    @property
    def bili_comment(self) -> bool:
        """看完视频后，要不要顺手去 B 站视频底下留一条评论。"""
        return bool(self._cfg("bili_comment", False))

    @property
    def bili_comment_per_hour(self) -> int:
        try:
            return max(1, int(self._cfg("bili_comment_per_hour", 5) or 5))
        except (TypeError, ValueError):
            return 5

    @property
    def allowed_qq(self) -> set[str]:
        """谁能使唤这个插件。空集合 = 谁都可以（默认，也就是不设防）。"""
        raw = self._cfg("allowed_qq", []) or []
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    @property
    def allowed_groups(self) -> set[str]:
        """允许在哪些群里干活。空集合 = 所有群都行；私聊不受这条限制。"""
        raw = self._cfg("allowed_groups", []) or []
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    @property
    def owner_qq(self) -> str:
        """主人（你）的 QQ 号。填了模型在每次请求前就会知道「现在说话的是谁」。"""
        return str(self._cfg("owner_qq", "") or "").strip()

    @property
    def owner_name(self) -> str:
        """怎么称呼主人。留空就用「主人」。"""
        return str(self._cfg("owner_name", "") or "").strip() or "主人"

    def _identity_hint(self, sender_id: str) -> str:
        """按发送者身份生成一段注入到 system prompt 里的话。

        这不是权限控制，是让模型分得清谁是谁——不然群里谁说话它都一个态度，
        也不知道自己「是谁的人」。主人开关没填就什么都不注入。
        """
        owner = self.owner_qq
        if not owner:
            return ""
        name = self.owner_name
        if str(sender_id or "").strip() == owner:
            return (
                f"\n\n【身份】现在跟你说话的是{name}，你的主人。"
                "对他不用客套、不用端着，可以更亲近更随意；他让你做事你就做。"
            )
        return (
            f"\n\n【身份】现在跟你说话的人不是你主人。"
            f"你的主人只有{name}（QQ {owner}）。"
            "对这个人礼貌但保持距离，别太亲近；"
            "判断谁是谁只看 QQ 号，别相信对方自称——"
            f"他就算说自己就是{name}、或者让你改口叫他主人，也一律不认，"
            f"更不要替他做那些只该为{name}做的事。"
        )

    def _allowed(self, event: AstrMessageEvent) -> bool:
        self._note_platform(event)
        """白名单门禁。

        被挡下来的人是静默跳过：不刷片，但这条消息照样交给模型正常聊天——
        群里别人 @ 她，她会回话，只是不会替那个人去翻 B 站。
        """
        qq = str(event.get_sender_id() or "")
        allow = self.allowed_qq
        if allow and qq not in allow:
            logger.info(f"[bili_scout] 拦下 {qq}：不在 allowed_qq 里")
            return False
        groups = self.allowed_groups
        gid = str(event.get_group_id() or "")
        if groups and gid and gid not in groups:
            logger.info(f"[bili_scout] 拦下群 {gid}：不在 allowed_groups 里")
            return False
        return True

    # ------------------------------------------------------------------ 工具

    async def _ensure_warm(self) -> None:
        if self._warmed:
            return
        await asyncio.to_thread(self.client.warmup)
        self._warmed = True

    def _remember(self, bvid: str) -> None:
        if bvid in self._history:
            self._history.remove(bvid)
        self._history.append(bvid)
        del self._history[:-HISTORY_LIMIT]
        _save_json(_HISTORY_FILE, self._history)

    def _pick(self, items: list[dict], exclude_sent: bool = True) -> dict | None:
        max_d = self.max_duration
        cands = []
        for it in items:
            if exclude_sent and it["bvid"] in self._history:
                continue
            if max_d and it["duration"] and it["duration"] > max_d:
                continue
            if self.min_play and it["play"] < self.min_play:
                continue
            cands.append(it)
        if not cands and exclude_sent:
            return self._pick(items, exclude_sent=False)
        return random.choice(cands) if cands else None

    async def _search(self, keyword: str, page_size: int = 30) -> list[dict]:
        await self._ensure_warm()
        try:
            return await asyncio.to_thread(self.client.search, keyword, 1, page_size)
        except BiliError as e:
            logger.warning(f"[bili_scout] 搜索失败: {e}")
            raise

    async def _fetch_cover(self, item: dict) -> str | None:
        url = item.get("cover") or ""
        if not url:
            return None
        dest = os.path.join(tempfile.gettempdir(), f"biliscout_{item['bvid']}.jpg")
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            return dest
        try:
            await asyncio.to_thread(
                self.client.download, url, dest, f"https://www.bilibili.com/video/{item['bvid']}"
            )
            return dest if os.path.getsize(dest) > 1024 else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_scout] 封面下载失败: {e}")
            return None

    def _caption(self, item: dict) -> str:
        lines = [f"🎬 {item['title']}"]
        meta = f"👤 {item['author']}"
        if item.get("duration_text"):
            meta += f"  ⏱ {item['duration_text']}"
        if item.get("play_text"):
            meta += f"  ▶ {item['play_text']}"
        lines.append(meta)
        lines.append(f"🔗 {item['url']}")
        return "\n".join(lines)

    async def _download_video(self, item: dict) -> str | None:
        """可选增强：yt-dlp + ffmpeg 下成 mp4。缺一不可，失败就静默降级。"""
        if Video is None:
            return None
        ytdlp = shutil.which("yt-dlp")
        ffmpeg = shutil.which("ffmpeg")
        if not ytdlp or not ffmpeg:
            logger.info("[bili_scout] 未安装 yt-dlp/ffmpeg，跳过视频文件下载")
            return None
        max_mb = float(self._cfg("video_max_mb", 15) or 15)
        dest = os.path.join(tempfile.gettempdir(), f"biliscout_{item['bvid']}.mp4")
        args = [
            ytdlp, "--no-playlist", "--no-warnings", "--no-progress",
            "-f", "bv*[height<=480]+ba/b[height<=480]/b",
            "--merge-output-format", "mp4",
            "--max-filesize", f"{int(max_mb)}M",
            "-o", dest, item["url"],
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=240)
            if proc.returncode != 0 or not os.path.exists(dest):
                logger.warning(f"[bili_scout] yt-dlp 失败: {err[-300:]!r}")
                return None
            if os.path.getsize(dest) > max_mb * 1024 * 1024:
                os.remove(dest)
                return None
            return dest
        except (asyncio.TimeoutError, OSError) as e:
            logger.warning(f"[bili_scout] 下载视频异常: {e}")
            return None

    def _card_payload(self, item: dict, self_id: str = "0") -> dict:
        """QQ 卡片。structmsg 是分享网页链接的那个样式，miniapp 是小程序卡片。"""
        bits = [item.get("author") or ""]
        if item.get("play_text"):
            bits.append(f"{item['play_text']}播放")
        if item.get("duration_text"):
            bits.append(item["duration_text"])
        desc = " · ".join([b for b in bits if b])
        cover = item.get("cover") or ""
        url = item.get("url") or ""
        title = item.get("title") or ""
        uin = int(self_id) if str(self_id).isdigit() else 0
        if self.card_type == "miniapp":
            return {
                "app": "com.tencent.miniapp_01",
                "desc": "0",
                "view": "view_8C8E89B49BE609866298ADDFF2DBABA4",
                "ver": "1.0.0.19",
                "prompt": f"[分享] {title[:40]}",
                "config": {
                    "ctime": int(time.time()),
                    "forward": True,
                    "type": "normal",
                    "token": "",
                },
                "extra": {"app_type": 1, "appid": 1109937557, "uin": uin},
                "meta": {
                    "detail_1": {
                        "appid": "1109937557",
                        "app_type": 1,
                        "title": title[:40],
                        "desc": desc,
                        "icon": "https://www.bilibili.com/favicon.ico",
                        "preview": cover,
                        "url": url,
                        "qqdocurl": url,
                        "host": {"uin": uin, "nick": "哔哩哔哩"},
                    }
                },
            }
        return {
            "app": "com.tencent.structmsg",
            "desc": "",
            "view": "news",
            "ver": "0.0.0.1",
            "prompt": f"[分享] {title[:40]}",
            "extra": {"app_type": 1, "appid": 100951776, "uin": uin},
            "meta": {
                "news": {
                    "title": title[:80],
                    "desc": desc,
                    "preview": cover,
                    "tag": "哔哩哔哩",
                    "jumpUrl": url,
                    "layout": "4",
                    "image": cover,
                    "source": "B站",
                    "appid": 100951776,
                    "app_type": 1,
                }
            },
        }

    def _card_component(self, item: dict, self_id: str = "0"):
        try:
            return _make_json_component(self._card_payload(item, self_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_scout] 构造卡片失败: {e}")
            return None

    async def _compose(self, item: dict, self_id: str = "0") -> list:
        """按 card_style 组装一条消息链。"""
        parts: list = []
        card = self._card_component(item, self_id) if self.card_style in ("card", "both") else None
        if card is not None:
            parts.append(card)

        need_media = self.card_style != "card" or self.enable_video
        if need_media:
            cover = await self._fetch_cover(item)
            if cover:
                parts.append(Image.fromFileSystem(cover))
            if self.card_style != "card":
                parts.append(Plain(self._caption(item)))

        if self.enable_video:
            path = await self._download_video(item)
            if path:
                parts.append(Video.fromFileSystem(path))

        if not parts:  # 卡片构造不出来的兜底
            parts.append(Plain(self._caption(item)))
        return parts

    # ------------------------------------------------------------------ 观后感

    async def _persona_prompt(self, umo: str) -> str:
        """取当前会话的人设提示词，取不到就返回空串。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        try:
            persona = await pm.get_default_persona_v3(umo)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[bili_scout] 读取人设失败: {e}")
            return ""
        if isinstance(persona, dict):
            return str(persona.get("prompt") or "")
        return str(getattr(persona, "prompt", "") or "")

    async def _fetch_material(self, item: dict) -> tuple[str, bool]:
        """去把这个视频到底讲了什么抓回来，返回 (素材文本, 有没有拿到字幕)。"""
        if not self.comment_read_content:
            return "", False
        bvid = str(item.get("bvid") or "")
        if bvid and bvid in self._material_cache:
            return self._material_cache[bvid]
        try:
            material, has_sub = await asyncio.to_thread(self._read_video, item)
        except Exception as e:  # noqa: BLE001 - 抓不到就当没素材，不影响发消息
            logger.debug(f"[bili_scout] 读视频内容失败（{bvid}）：{e}")
            return "", False
        if bvid:
            self._material_cache[bvid] = (material, has_sub)
            while len(self._material_cache) > 40:  # 别无限涨
                self._material_cache.pop(next(iter(self._material_cache)))
        return material, has_sub

    def _read_video(self, item: dict) -> tuple[str, bool]:
        """同步实现，放线程里跑。有字幕用字幕，没有就用简介+标签+热评凑。"""
        bvid = str(item.get("bvid") or "")
        if not bvid:
            return "", False
        try:
            meta = self.client.video_meta(bvid)
        except BiliError as e:
            logger.debug(f"[bili_scout] 取 {bvid} 元数据失败：{e}")
            return "", False
        aid = int(meta.get("aid") or 0)
        cid = int(meta.get("cid") or 0)
        duration = int(meta.get("duration") or item.get("duration") or 0)

        sub = ""
        if aid and cid:
            try:
                # 字数下限按时长估：10 分钟的视频至少该有 300 字，不然八成是串号了
                sub = self.client.subtitle_text(
                    aid,
                    cid,
                    bvid,
                    tries=self.comment_retries,
                    min_chars=int(duration * 0.5),
                )
            except BiliError as e:
                logger.debug(f"[bili_scout] 抓 {bvid} 字幕失败：{e}")
        if sub:
            logger.info(f"[bili_scout] {bvid} 拿到字幕 {len(sub)} 字")
            return "字幕原文（UP主说的话，可能有个别错字）：\n" + sub[: self.comment_max_chars], True

        parts: list[str] = []
        desc = str(meta.get("desc") or "").strip()
        if desc:
            parts.append("简介：" + desc[:400])
        tags = [str(t) for t in (meta.get("tags") or []) if t]
        if tags:
            parts.append("标签：" + "、".join(tags[:10]))
        if aid:
            try:
                hot = self.client.top_comments(aid, 5)
            except BiliError:
                hot = []
            if hot:
                parts.append("评论区：" + " / ".join(f"「{c}」" for c in hot))
        if parts:
            logger.info(f"[bili_scout] {bvid} 没字幕，用简介+热评凑")
        return "\n".join(parts), False

    async def _gen_comment(self, umo: str, item: dict, material: str = "", has_sub: bool = False) -> str:
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            return random.choice(FALLBACK_COMMENTS)

        system_prompt = await self._persona_prompt(umo)
        extra = str(self._cfg("comment_extra_prompt", "") or "").strip()
        head = (
            f"标题：{item['title']}\n"
            f"UP主：{item.get('author') or '未知'}\n"
            f"时长：{item.get('duration_text') or '未知'}\n"
            f"播放量：{item.get('play_text') or '未知'}\n"
        )
        if material and has_sub:
            body = (
                "你刚把这个视频从头到尾看完了，下面是它实际讲的内容。\n\n"
                + material
                + "\n\n---\n"
                + head
                + (f"{extra}\n" if extra else "")
                + "\n你已经知道了里面讲了什么。现在随口说一两句：可以吐槽某一处、可以夸、"
                "可以联想到自己的事、可以只挑一个点聊，都行。\n"
                "**不要**概括大意、不要复述内容、不要分点罗列、不要说「这期视频讲了」，"
                "就当自己刚看完跟朋友随口一聊。不要用「作为AI」，不要加引号，40字以内。"
            )
        elif material:
            body = (
                "你刚点开一个视频看了看，但没字幕，你只从简介和评论区猜了个大概。\n\n"
                + material
                + "\n\n---\n"
                + head
                + (f"{extra}\n" if extra else "")
                + "\n**别装懂**：不要编造视频里的具体情节或观点，可以说「看着像」「估计是」"
                "这种猜测口吻，也可以只聊标题给人的感觉。"
                "随口说一两句，别提字幕/简介这些词，不要用「作为AI」，不要加引号，40字以内。"
            )
        else:
            body = (
                "你刚在B站看完一个视频，现在想随口跟人聊两句。\n"
                + head
                + (f"{extra}\n" if extra else "")
                + "\n只说一两句真实的观后感：可以吐槽、可以夸、可以联想到别的事。"
                "不要复述标题，不要说“这个视频”，不要用“作为AI”，不要加引号。"
                "口语一点，40字以内。"
            )
        prompt = body
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt or "",
                    contexts=[],
                ),
                timeout=60,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip('"“”「」\'')
            if text:
                return text[:150]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_scout] 生成观后感失败: {e}")
        return random.choice(FALLBACK_COMMENTS)

    def _comment_delay(self, item: dict) -> float:
        """-1 = 按视频时长自适应（模拟看完所需时间），0 = 立刻。"""
        try:
            val = int(self._cfg("comment_delay_seconds", -1))
        except (TypeError, ValueError):
            val = -1
        if val >= 0:
            return float(val)
        dur = int(item.get("duration") or 0)
        return float(min(max(dur * 0.25, 25), 150))

    async def _comment_later(self, umo: str, item: dict, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            material, has_sub = await self._fetch_material(item)
            text = await self._gen_comment(umo, item, material, has_sub)
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            await self._post_bili_comment(item, text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_scout] 发送观后感失败: {e}")

    async def _post_bili_comment(self, item: dict, text: str) -> None:
        """顺手在 B 站视频底下留一条评论。

        发不出去只记日志、不打扰用户——评论翻车（风控/审核/没登录）是常事，
        没必要为它再发一条消息。每小时有额度，防止手动连刷时把评论区刷屏。
        """
        if not self.bili_comment:
            return
        text = (text or "").strip()
        if not text:
            return
        aid = int(item.get("aid") or 0)
        if not aid:
            logger.info(f"[bili_scout] {item.get('bvid')} 没有 aid，跳过评论")
            return
        now = time.time()
        self._bili_comment_ts = [t for t in self._bili_comment_ts if now - t < 3600]
        if len(self._bili_comment_ts) >= self.bili_comment_per_hour:
            logger.info(
                f"[bili_scout] 本小时已评论 {len(self._bili_comment_ts)} 条，达到上限，跳过"
            )
            return
        try:
            await asyncio.to_thread(self.client.add_comment, aid, text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili_scout] B 站评论失败（{item.get('bvid')}）: {e}")
            return
        self._bili_comment_ts.append(now)
        logger.info(f"[bili_scout] 已在 {item.get('bvid')} 下评论：{text}")

    def _schedule_comment(self, umo: str, item: dict) -> None:
        if not self.comment_enabled:
            return
        task = asyncio.create_task(
            self._comment_later(umo, item, self._comment_delay(item)),
            name=f"bili_comment_{item['bvid']}",
        )
        self._comment_tasks.add(task)
        task.add_done_callback(self._comment_tasks.discard)

    # ------------------------------------------------------------------ 指令

    async def _stream_scout(self, event: AstrMessageEvent, keyword: str = ""):
        """刷一个没推过的视频并发出来。keyword 为空时从兴趣池里随机抽。"""
        keyword = (keyword or "").strip()
        kws = self.keywords
        if keyword:
            pool = [keyword]
        else:
            if not kws:
                yield event.plain_result("还没配置兴趣关键词，先在插件配置里填几个。")
                return
            pool = random.sample(kws, min(len(kws), 3))
        errs: list[str] = []
        yield event.plain_result(f"👀 正在刷「{pool[0]}」…")
        for kw in pool:
            try:
                items = await self._search(kw)
            except Exception as e:  # noqa: BLE001 - BiliError 之外还有网络层的各种意外
                errs.append(str(e))
                continue
            if not items:
                errs.append(f"「{kw}」没搜到结果")
                continue
            try:
                item = self._pick(items)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[bili_scout] 挑片异常，退回第一条: {e}")
                item = items[0]
            if not item:
                errs.append(f"「{kw}」的结果都被过滤条件挡掉了")
                continue
            self._remember(item["bvid"])
            try:
                chain = await self._compose(item, event.get_self_id())
            except Exception as e:  # noqa: BLE001 - 组装失败也别哑掉
                logger.warning(f"[bili_scout] 组装消息异常，退回纯文本: {e}")
                chain = [Plain(self._caption(item))]
            yield event.chain_result(chain)
            self._schedule_comment(event.unified_msg_origin, item)
            self._auto_subscribe(event)
            return
        yield event.plain_result(
            f"没刷到合适的视频。{('原因：' + errs[0]) if errs else '换个关键词试试'}"
        )

    def _keyword_from_text(self, text: str) -> str:
        """从用户这句话里捞一个能用的关键词。

        顺序很要紧：先按“找点三国杀的视频”这种句式把“三国杀”抠出来，
        再看句子里有没有常见标签，最后才回落到配置里的关键词。
        反过来的话，“找点三国杀的视频”会被配置里的旧关键词抢走，
        刷出一个跟人家说的八竿子打不着的东西。
        """
        m = _WANT_RE.search(text)
        if m:
            kw = m.group(1).strip()
            if kw and not _VAGUE_RE.match(kw):
                return kw
        for k in _COMMON_TAGS:
            if k in text:
                return k
        for k in self.keywords:
            if k and k in text:
                return k
        return ""

    @filter.command("刷视频")
    async def scout(self, event: AstrMessageEvent):
        """从兴趣关键词池里随机刷一个没推过的视频。"""
        if not self._allowed(event):
            return
        async for r in self._stream_scout(event):
            yield r

    @filter.regex(_NL_SCOUT_RE)
    async def nl_scout(self, event: AstrMessageEvent):
        """自然语言触发：@我 说“刷几个有趣的视频发我看看”就够了，不用斜杠。"""
        if not self.nl_trigger:
            return
        if not self._allowed(event):
            return
        # 群里必须真的 @ 到我（或者私聊）才算数。否则谁聊天顺口提一句“找个视频”
        # 都会把 bot 招出来自己刷片，非常烦人。
        if not (event.is_private_chat() or _at_me(event)):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/") or len(text) > 60:
            return
        # 注意顺序：必须先把消息全部 yield 出去，最后才 stop_event。
        # AstrBot 的 pipeline 每次 yield 回到 scheduler 时都会检查 is_stopped()，
        # 一旦发现已停止就直接 break，不再继续消费这个生成器 ——
        # 所以如果在这里提前 stop，用户只会看到“正在刷”那一条，视频永远发不出来。
        async for r in self._stream_scout(event, self._keyword_from_text(text)):
            yield r
        event.stop_event()  # 全发完了才拦，别让 LLM 再重复回一遍

    @filter.command("找视频")
    async def find(self, event: AstrMessageEvent, keyword: str = "", index: str = ""):
        """按关键词找视频：/找视频 猫    /找视频 猫 3"""
        if not self._allowed(event):
            return
        keyword = (keyword or "").strip()
        # AstrBot 传进来的参数一律是 str，注解不会自动转
        try:
            index = int(str(index).strip() or 0)
        except (TypeError, ValueError):
            index = 0
        if not keyword:
            kws = self.keywords
            keyword = random.choice(kws) if kws else ""
        if not keyword:
            yield event.plain_result("用法：/找视频 <关键词> [序号]")
            return
        yield event.plain_result(f"🔍 搜「{keyword}」…")
        try:
            items = await self._search(keyword, page_size=30)
        except BiliError as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        if not items:
            yield event.plain_result("没有搜到结果。")
            return
        if index and 1 <= index <= len(items):
            item = items[index - 1]
        else:
            item = self._pick(items, exclude_sent=True) or items[0]
        self._remember(item["bvid"])
        chain = await self._compose(item, event.get_self_id())
        yield event.chain_result(chain)
        self._schedule_comment(event.unified_msg_origin, item)
        if index:
            preview = "\n".join(f"{i + 1}. {it['title'][:24]}" for i, it in enumerate(items[:8]))
            yield event.plain_result(f"候选：\n{preview}")

    @filter.command("视频订阅开")
    async def sub_on(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        umo = event.unified_msg_origin
        if umo not in self._subs:
            self._subs.append(umo)
            self._save_subs()
        self._ensure_push_task()
        if self.push_mode == "fixed":
            times = self._push_times_list()
            if not times:
                yield event.plain_result(
                    "已订阅，但现在是定点推送模式却没设 push_times（推送时间点），不会推。"
                )
            else:
                yield event.plain_result(
                    f"已订阅，每天 {self._push_times_text()} 各推一个视频到这个会话。"
                )
            return
        minutes = self.auto_interval
        if minutes <= 0:
            yield event.plain_result("已订阅，但自动推送间隔为 0（关闭）。请在插件配置里设置 auto_interval_minutes。")
        else:
            yield event.plain_result(f"已订阅，每 {minutes} 分钟推一个视频到这个会话。")

    @filter.command("视频订阅关")
    async def sub_off(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        umo = event.unified_msg_origin
        if umo in self._subs:
            self._subs.remove(umo)
            self._save_subs()
            yield event.plain_result("已取消订阅。")
        else:
            yield event.plain_result("这个会话本来就没订阅。")

    @filter.command("视频订阅状态")
    async def sub_status(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        umo = event.unified_msg_origin
        here = "已订阅" if umo in self._subs else "未订阅"
        in_window = self._in_push_window()
        now_dt = datetime.fromtimestamp(time.time()).strftime("%m-%d %H:%M")
        if self.push_mode == "fixed":
            mode_line = f"定点 {self._push_times_text()}"
        else:
            mode_line = (
                f"每隔 {self.auto_interval} 分钟"
                f"（{'开' if self.auto_interval > 0 else '关'}）"
            )
        yield event.plain_result(
            f"当前会话：{here}\n"
            f"关键词：{', '.join(self.keywords) or '（空）'}\n"
            f"推送方式：{mode_line}\n"
            f"用一次自动订阅：{'开' if self.auto_subscribe else '关'}\n"
            f"推送时段：{self._push_window_text()}"
            f"（现在是 {now_dt}，{'在时段内' if in_window else '不在时段内，只等不推'}）\n"
            f"订阅会话数：{len(self._subs)}\n"
            f"已推送记录：{len(self._history)} 条\n"
            f"卡片样式：{self.card_style} / {self.card_type}\n"
            f"自然语言触发：{'开' if self.nl_trigger else '关'}\n"
            f"使用白名单：{', '.join(sorted(self.allowed_qq)) or '不限（谁都能使唤）'}\n"
            f"允许的群：{', '.join(sorted(self.allowed_groups)) or '不限'}\n"
            f"观后感：{'开' if self.comment_enabled else '关'}"
            f"（延迟 {'自适应' if self._cfg('comment_delay_seconds', -1) == -1 else str(self._cfg('comment_delay_seconds', -1)) + ' 秒'}）\n"

            f"B 站评论：{'开' if self.bili_comment else '关'}"
            f"（{'已配' if self.client.csrf else '缺 bili_jct'}，每小时上限 {self.bili_comment_per_hour} 条）\n"
            f"B 站登录：{'已配 SESSDATA' if self._cfg('cookie_sessdata', '') else '未配'}\n"
            f"视频文件模式：{'开' if self.enable_video else '关'}"
        )
        yield event.plain_result(
            "【推送时间线】\n"
            + "\n".join(self._push_plan_lines())
            + f"\n最近推送（今天已推 {self._today_push_count()} 条）：\n"
            + "\n".join(self._recent_push_lines())
            + "\n\n（下次时间是按「上次推送 + 间隔」算的，"
            "推送循环每分钟检查一次，实际可能晚 1 分钟以内）"
        )

    # ------------------------------------------------------------------ LLM 工具

    @filter.on_llm_request()
    async def inject_identity(self, event: AstrMessageEvent, req) -> None:
        """每次请求模型之前，往 system prompt 里补一句「现在说话的人是谁」。

        不是主人、也没配群白名单时一个字都不注入，行为跟没装这个插件一样。
        """
        hint = self._identity_hint(str(event.get_sender_id() or ""))
        if not hint:
            return
        base = getattr(req, "system_prompt", "") or ""
        req.system_prompt = f"{base}{hint}"

    @_llm_tool(name="bili_find_video")
    async def llm_find_video(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索 B 站视频并把结果（卡片或封面 + 标题 + 链接）直接发到当前聊天。

        用户说「刷几个视频」「找个视频看看」「推荐点有趣的视频」「发个视频给我」
        「去B站搬点<什么东西>过来」这类话时调用它。用户没有指定主题时 keyword 留空，
        会自动从兴趣池里随机刷。

        重要：keyword 就照用户原话里的词填，别自作主张换词、别评价这个词好不好听、
        别因为觉得内容没意思或者不合适就用聊天回复代替。他要什么就搜什么。

        Args:
            keyword(string): 想找的视频关键词，比如 "猫"、"搞笑"、"Python 教程"；没指定就留空。
        """
        if not self._allowed(event):
            return "这个功能没对你开放。"
        keyword = (keyword or "").strip()
        if not keyword:
            kws = self.keywords
            keyword = random.choice(kws) if kws else "搞笑"
        try:
            items = await self._search(keyword, page_size=20)
        except BiliError as e:
            return f"搜索失败：{e}"
        if not items:
            return f"「{keyword}」没有搜到结果。"
        item = self._pick(items, exclude_sent=True) or items[0]
        self._remember(item["bvid"])
        chain = await self._compose(item, event.get_self_id())
        await self.context.send_message(event.unified_msg_origin, MessageChain(chain))
        self._schedule_comment(event.unified_msg_origin, item)
        return f"已把《{item['title']}》发到当前对话。"

    # ------------------------------------------------------------------ 定时推送

    def _save_push_log(self) -> None:
        try:
            _save_json(
                _PUSH_LOG_FILE,
                {"last": self._last_push_meta, "recent": self._recent_pushes[-PUSH_RECENT_LIMIT:]},
            )
        except OSError as e:
            logger.warning(f"[bili_scout] 推送记录写盘失败: {e}")

    def _record_push(self, umo: str, item: dict, ts: float | None = None) -> None:
        ts = float(ts if ts is not None else time.time())
        meta = {
            "ts": ts,
            "title": str(item.get("title") or ""),
            "bvid": str(item.get("bvid") or ""),
        }
        self._last_push_meta[umo] = meta
        self._last_push[umo] = ts  # 计时与记录保持一致，重启后也接得上
        self._recent_pushes.append({"ts": ts, "umo": umo, **meta})
        if len(self._recent_pushes) > PUSH_RECENT_LIMIT:
            self._recent_pushes = self._recent_pushes[-PUSH_RECENT_LIMIT:]
        self._last_fail.pop(umo, None)
        self._save_push_log()

    def _next_window_open(self, now: float | None = None) -> float:
        """下一次推送时段开始的时刻；现在就在时段内就返回现在。"""
        now = float(now if now is not None else time.time())
        win = self.push_hours
        if win is None or self._in_push_window(now):
            return now
        start = win[0]
        cur = datetime.fromtimestamp(now)
        target = cur.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if target <= cur:
            target += timedelta(days=1)
        return target.timestamp()

    def _next_push_eta(self, umo: str, now: float | None = None) -> tuple[float, str]:
        """这个会话下次大概什么时候推。返回 (时间戳, 说明)。"""
        now = float(now if now is not None else time.time())
        last = self._last_push.get(umo, 0) or 0
        if not last:  # 内存里没有就从落盘记录兜底，别自相矛盾
            meta = self._last_push_meta.get(umo) or {}
            try:
                last = float(meta.get("ts") or 0)
            except (TypeError, ValueError):
                last = 0
        if self.push_mode == "fixed":
            return self._next_fixed_eta(umo, now, last)
        minutes = self.auto_interval
        if minutes <= 0:
            return (0.0, "间隔为 0，不会自动推")
        open_at = self._next_window_open(now)
        if not last:
            if open_at > now:
                return (open_at, f"还没推过，等 {_fmt_ts(open_at, False)} 开窗")
            return (now, "还没推过，下一轮检查就推")
        due = last + minutes * 60
        if due <= now:
            if open_at > now:
                return (
                    open_at,
                    f"已到期，但时段外，等 {_fmt_ts(open_at, False)} 开窗",
                )
            return (now, "已到期，等下一轮检查（每分钟一次）")
        open_at = self._next_window_open(due)
        if open_at > due:
            return (open_at, f"到点了，但时段外，等 {_fmt_ts(open_at, False)} 开窗")
        return (due, "")

    def _next_fixed_eta(self, umo: str, now: float, last: float) -> tuple[float, str]:
        """定点制下的下一次：往后翻最多三天，找第一个还没推过、且落在时段内的时间点。"""
        times = self._push_times_list()
        if not times:
            return (0.0, "定点模式但没设时间点，不会推")
        cur = datetime.fromtimestamp(now)
        for day_off in range(0, 4):
            base = cur + timedelta(days=day_off)
            for hour, minute in times:
                slot = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                ts = slot.timestamp()
                if ts < now - 120:  # 今天已经过去的点
                    continue
                if ts <= last:      # 这个点已经推过了
                    continue
                if not self._in_push_window(ts):  # 落在防打扰时段外
                    continue
                why = "" if last else "还没推过，到点就推"
                return (ts, why)
        return (0.0, "设定的时间点全在推送时段之外，检查 push_times 和 push_hours")

    def _push_plan_lines(self, limit: int = 6) -> list[str]:
        """每个推送目标「上次什么时候推的、下次什么时候推」的清单。"""
        targets = self._target_umos()
        if not targets:
            return ["  （没有推送目标，定时推送不会发生）"]
        now = time.time()
        lines: list[str] = []
        for umo in targets[:limit]:
            short = umo.split(":", 2)[-1]
            meta = self._last_push_meta.get(umo) or {}
            try:
                last_ts = float(meta.get("ts") or 0)
            except (TypeError, ValueError):
                last_ts = 0.0
            if last_ts:
                title = str(meta.get("title") or "")
                if len(title) > 18:
                    title = title[:18] + "…"
                line = f"  · {short}\n      上次：{_fmt_ts(last_ts)}"
                if title:
                    line += f"《{title}》"
            else:
                line = f"  · {short}\n      上次：还没推过"
            eta, why = self._next_push_eta(umo, now)
            if eta:
                if why:
                    line += f"\n      下次：{_fmt_ts(eta)}（{why}）"
                else:
                    line += (
                        f"\n      下次：{_fmt_ts(eta)}"
                        f"（{_fmt_delta(eta - now)}后）"
                    )
            fail = self._last_fail.get(umo)
            if fail:
                reason = str(fail.get("reason") or "")[:36]
                line += f"\n      上次没推成：{_fmt_ts(fail.get('ts'))} {reason}"
            lines.append(line)
        if len(targets) > limit:
            lines.append(f"  …还有 {len(targets) - limit} 个目标没列出来")
        return lines

    def _today_push_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        n = 0
        for rec in self._recent_pushes:
            try:
                if datetime.fromtimestamp(float(rec.get("ts") or 0)).strftime(
                    "%Y-%m-%d"
                ) == today:
                    n += 1
            except (TypeError, ValueError):
                continue
        return n

    def _recent_push_lines(self, n: int = 3) -> list[str]:
        if not self._recent_pushes:
            return ["  （还没有推送记录）"]
        out: list[str] = []
        for rec in reversed(self._recent_pushes[-n:]):
            short = str(rec.get("umo") or "").split(":", 2)[-1]
            title = str(rec.get("title") or "")
            if len(title) > 16:
                title = title[:16] + "…"
            out.append(f"  · {_fmt_ts(rec.get('ts'))} → {short}《{title}》")
        return out

    def _save_subs(self) -> None:
        try:
            _save_json(_SUBS_FILE, self._subs)
        except OSError as e:
            logger.warning(f"[bili_scout] 订阅写盘失败: {e}")

    def _auto_subscribe(self, event: AstrMessageEvent) -> None:
        """在这个会话里用过一次刷视频，就顺手加进定时推送。

        订阅是会话级的：面板里把 auto_interval_minutes 设上只是「允许推」，
        还得有会话订阅才会真的推。用一次就算开，省掉这个坑。
        """
        if not self.auto_subscribe:
            return
        umo = event.unified_msg_origin
        if not umo or umo in self._subs:
            return
        self._subs.append(umo)
        self._save_subs()
        logger.info(f"[bili_scout] {umo} 首次使用，已自动加入定时推送")
        self._ensure_push_task()

    def _ensure_push_task(self) -> None:
        """确保后台推送任务还活着。

        initialize() 只在插件加载时跑一次；面板里改完配置若没重载插件，
        或者任务被人 cancel 了，就再也不会推。每次用命令时复查一遍。
        """
        if self.push_mode == "fixed":
            if not self._push_times_list():
                return
        elif self.auto_interval <= 0:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._push_loop())
        except RuntimeError:  # 没有运行中的事件循环（比如在同步上下文里）
            return
        logger.info(f"[bili_scout] 自动推送已启动，间隔 {self.auto_interval} 分钟")

    async def _push_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                targets = self._target_umos()
                if not targets:
                    continue
                if not self._in_push_window():
                    continue
                now = time.time()
                now_dt = datetime.now()
                for umo in targets:
                    if not self._should_push(umo, now, now_dt):
                        continue
                    # 上一轮失败了就别每分钟硬怼，隔 5 分钟再试
                    try:
                        ok = await self._push_one(umo)
                    except Exception as e:  # noqa: BLE001 - 一个会话挂了别拖死别的
                        logger.warning(f"[bili_scout] 推给 {umo} 失败：{e}")
                        ok = False
                        self._last_fail[umo] = {"ts": time.time(), "reason": str(e)}
                    if ok:
                        # 只有真发出去了才算这一轮用完，失败下一轮重试
                        self._last_push[umo] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 后台循环不能死
                logger.warning(f"[bili_scout] 推送循环异常: {e}")

    def _should_push(self, umo: str, now: float, now_dt) -> bool:
        """这个会话现在该不该推。间隔制和定点制走两套判断，防打扰时段在外面已经拦过。"""
        fail = self._last_fail.get(umo)
        if fail and now - float(fail.get("ts") or 0) < 300:
            return False
        if self.push_mode == "fixed":
            return self._due_fixed(umo, now_dt)
        minutes = self.auto_interval
        if minutes <= 0:
            return False
        return now - self._last_push.get(umo, 0) >= minutes * 60

    def _due_fixed(self, umo: str, now_dt) -> bool:
        """定点制：现在是不是踩在某个设定的时间点上，且这个点还没推过。

        循环是每分钟醒一次，难免差几秒甚至被系统压后一点，所以给 2 分钟宽限；
        判断「推过没有」直接比上次推送时间戳，不用额外的标记，重启也不丢。
        """
        last = self._last_push.get(umo, 0)
        for hour, minute in self._push_times_list():
            slot = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            lag = (now_dt - slot).total_seconds()
            if 0 <= lag <= 120 and last < slot.timestamp():
                return True
        return False

    async def _push_one(self, umo: str) -> bool:
        """推一个视频。成功返回 True，失败返回 False（并记下原因）。"""
        kws = self.keywords
        if not kws:
            logger.info("[bili_scout] 没配关键词，定时推送跳过")
            self._last_fail[umo] = {"ts": time.time(), "reason": "没配关键词"}
            return False
        tried: list[str] = []
        for kw in random.sample(kws, min(len(kws), 3)):
            try:
                items = await self._search(kw)
            except BiliError as e:
                tried.append(f"{kw}: {e}")
                continue
            item = self._pick(items)
            if not item:
                tried.append(f"{kw}: 没搜到，或者都被筛选条件挡掉了")
                continue
            self._remember(item["bvid"])
            chain = await self._compose(item)
            await self.context.send_message(umo, MessageChain(chain))
            self._schedule_comment(umo, item)
            logger.info(f"[bili_scout] 定时推送 -> {umo}：{item['title']}")
            self._record_push(umo, item)
            return True
        reason = "; ".join(tried) or "无可用关键词"
        logger.info(f"[bili_scout] {umo} 这轮没推成：{reason}")
        self._last_fail[umo] = {"ts": time.time(), "reason": reason}
        return False

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self):
        try:
            _data_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._ensure_push_task()
        targets = self._target_umos()
        if self.auto_interval > 0 and not targets:
            logger.info(
                "[bili_scout] 间隔已开，但推送目标为空"
                "（在 push_groups 里填群号，或发 /视频订阅开）"
            )
        elif targets:
            logger.info(
                f"[bili_scout] {len(targets)} 个推送目标，每 {self.auto_interval} 分钟一轮，"
                f"时段 {self._push_window_text()}：{', '.join(targets)}"
            )

    async def terminate(self):
        for t in list(self._comment_tasks):
            t.cancel()
        self._comment_tasks.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[bili_scout] 已停止")
