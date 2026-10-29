"""B 站搜索 / 详情 / 封面的一层薄封装，只依赖标准库。

设计要点：
- 搜索接口需要 wbi 签名 + 主页 cookie(buvid3) + 搜索页 Referer，三者缺一不可；
- 无登录态拿不到播放地址（CDN 403），所以这里只管元数据和封面；
- 所有请求都是同步阻塞的，调用方负责丢进线程池。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

# wbi 的字符重排表，B 站前端写死的那张
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_TAG_RE = re.compile(r"<[^>]+>")


class BiliError(Exception):
    pass


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("&quot;", '"').strip()


def _fmt_play(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _parse_duration(text) -> int:
    """'9:42' / '1:02:03' -> 秒。"""
    if isinstance(text, int):
        return text
    parts = str(text or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for v in nums:
        sec = sec * 60 + v
    return sec


class BiliClient:
    def __init__(self, sessdata: str = "", bili_jct: str = "", timeout: int = 15):
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self._mixin_key = ""
        self._mixin_ts = 0.0
        self.csrf = (bili_jct or "").strip()
        if sessdata:
            self._set_cookie("SESSDATA", sessdata)
        if self.csrf:
            self._set_cookie("bili_jct", self.csrf)

    # ---------- cookie / session ----------

    def _set_cookie(self, name: str, value: str) -> None:
        import http.cookies

        c = http.cookies.SimpleCookie()
        c[name] = value
        morsel = c[name]
        cookie = http.cookiejar.Cookie(
            version=0, name=name, value=morsel.value, port=None, port_specified=False,
            domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=False, expires=None,
            discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        self.cj.set_cookie(cookie)

    def _raw(self, url: str, referer: str = "https://www.bilibili.com/", binary: bool = False):
        headers = {
            "User-Agent": _UA,
            "Referer": referer,
            "Accept": "*/*" if binary else "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise BiliError(f"HTTP {e.code} @ {url.split('?')[0]}") from e
        except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
            raise BiliError(f"网络错误: {e}") from e
        return data if binary else data.decode("utf-8", "replace")

    def _json(self, url: str, referer: str = "https://www.bilibili.com/") -> dict:
        body = self._raw(url, referer=referer)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise BiliError("接口返回的不是 JSON（大概率被风控了）") from e

    def warmup(self) -> None:
        """拿 buvid3，并缓存 wbi 的 mixin key。空 cookie 直接搜会被 412 挡。"""
        self._raw("https://www.bilibili.com/")
        self._refresh_mixin(force=True)

    def _refresh_mixin(self, force: bool = False) -> str:
        if not force and self._mixin_key and time.time() - self._mixin_ts < 3600:
            return self._mixin_key
        nav = self._json("https://api.bilibili.com/x/web-interface/nav")
        try:
            wbi = nav["data"]["wbi_img"]
            img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
            sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        except (KeyError, TypeError) as e:
            raise BiliError("拿不到 wbi 签名密钥，B 站接口可能又变了") from e
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in _MIXIN_TAB)[:32]
        self._mixin_ts = time.time()
        return self._mixin_key

    def _signed(self, params: dict) -> str:
        params = dict(params)
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        w_rid = hashlib.md5((query + self._refresh_mixin()).encode()).hexdigest()
        return f"{query}&w_rid={w_rid}"

    # ---------- 业务 ----------

    def search(self, keyword: str, page: int = 1, page_size: int = 20) -> list[dict]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        query = self._signed({
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
        })
        url = f"https://api.bilibili.com/x/web-interface/wbi/search/type?{query}"
        # Referer 必须指向带同一个 keyword 的搜索页，否则返回 v_voucher 风控挑战
        referer = "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword)
        payload = self._json(url, referer=referer)
        if payload.get("code") != 0:
            raise BiliError(f"搜索失败：{payload.get('message') or payload.get('code')}")
        data = payload.get("data") or {}
        if "v_voucher" in data and not data.get("result"):
            raise BiliError("触发 B 站风控校验（v_voucher），稍后重试或配置 SESSDATA")
        items = []
        for it in data.get("result") or []:
            bvid = it.get("bvid")
            if not bvid:
                continue
            items.append({
                "bvid": bvid,
                "aid": int(it.get("aid") or 0),
                "title": _clean(it.get("title")),
                "author": _clean(it.get("author")),
                "duration_text": it.get("duration") or "",
                "duration": _parse_duration(it.get("duration")),
                "play": int(it.get("play") or 0),
                "play_text": _fmt_play(it.get("play")),
                "pubdate": int(it.get("pubdate") or 0),
                "cover": ("https:" + it["pic"]) if str(it.get("pic", "")).startswith("//") else it.get("pic", ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
            })
        return items

    def video_meta(self, bvid: str) -> dict:
        """拿理解内容要用的元数据：aid/cid/简介/标签。

        video_detail 是给卡片用的（只要展示字段），这个是给模型看的，
        所以额外要 aid（评论和字幕的 id）、cid（字幕挂在这上面）和标签。
        """
        ref = f"https://www.bilibili.com/video/{bvid}/"
        payload = self._json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", ref)
        if payload.get("code") != 0:
            raise BiliError(f"取详情失败：{payload.get('message')}")
        d = payload.get("data") or {}
        tags: list[str] = []
        try:
            t = self._json(f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}", ref)
            tags = [str(x.get("tag_name")) for x in (t.get("data") or []) if x.get("tag_name")]
        except BiliError:
            pass  # 标签是锦上添花，拿不到不影响
        return {
            "aid": int(d.get("aid") or 0),
            "cid": int(d.get("cid") or 0),
            "desc": _clean(d.get("desc"))[:500],
            "tags": tags[:10],
            "duration": int(d.get("duration") or 0),
            "tname": _clean(d.get("tname")),
        }

    def subtitle_candidates(self, aid: int, cid: int, bvid: str = "", zh_only: bool = False) -> list[dict]:
        """列出这个视频可下载的字幕，中文排前面。

        zh_only=True 时只留中文和纯音乐之外的语种——英文 AI 字幕一样会串号，
        对中文视频也没用，不值得为它冒险。
        """
        ref = f"https://www.bilibili.com/video/{bvid}/" if bvid else "https://www.bilibili.com/"
        payload = self._json(
            f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}", ref
        )
        subs = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        out: list[dict] = []
        for s in subs:
            url = str(s.get("subtitle_url") or "").strip()
            if not url:  # 没 url 的是锁定字幕（UP 主没开下载），跳过
                continue
            if url.startswith("//"):
                url = "https:" + url
            lan = str(s.get("lan") or "")
            out.append({"lan": lan, "url": url, "is_ai": int(s.get("type") or 0) == 1})
        if zh_only:
            out = [x for x in out if x["lan"].startswith(("ai-zh", "zh"))]
        out.sort(key=lambda x: 0 if x["lan"].startswith(("ai-zh", "zh")) else 1)
        return out

    def fetch_subtitle(self, url: str, bvid: str = "") -> str:
        """下载一份字幕，拼成整段文字。"""
        ref = f"https://www.bilibili.com/video/{bvid}/" if bvid else "https://www.bilibili.com/"
        payload = self._json(url, ref)
        body = payload.get("body") or []
        return "".join(str(line.get("content") or "").strip() for line in body)

    def subtitle_text(
        self, aid: int, cid: int, bvid: str = "", tries: int = 6, min_chars: int = 0
    ) -> str:
        """取字幕全文，带反串号过滤。

        坑：B 站 AI 字幕接口返回的 CDN 地址会串号——同一个视频反复请求，
        大部分时候给的是**别的视频**的字幕（拿到过音乐合集、车祸新闻、悬案解说…），
        只有约三分之一是对的。直接信它，机器人就会对着三国杀的视频点评苹果手机。

        判别办法：**正确的那个地址是稳定的，错的每次都不一样**。所以只取中文候选、
        反复请求、统计去掉 auth_key 后的文件路径，同一个路径出现两次才认。
        一次都没撞到重复的，就当这个视频没字幕——宁可不说，也不能胡说。
        """
        seen: dict[str, list[str]] = {}
        rejected: set[str] = set()  # 重复出现但内容明显不对的地址，别反复下
        for _ in range(max(2, int(tries))):
            for cand in self.subtitle_candidates(aid, cid, bvid, zh_only=True):
                key = cand["url"].split("?")[0]  # 同一份文件的不同签名归为一类
                if key in rejected:
                    continue
                seen.setdefault(key, []).append(cand["url"])
                if len(seen[key]) < 2:
                    continue
                try:
                    text = self.fetch_subtitle(cand["url"], bvid)
                except BiliError:
                    rejected.add(key)
                    continue
                if min_chars and len(text) < min_chars:
                    # 对上了同一份文件，但字数跟视频时长差太远，八成也是别人的
                    rejected.add(key)
                    continue
                return text
        return ""

    def top_comments(self, aid: int, limit: int = 8) -> list[str]:
        """拿几条热评，字幕缺失时用来猜视频内容。"""
        n = max(1, min(int(limit), 20))
        url = (
            f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}"
            f"&sort=2&ps={n}&pn=1"
        )
        payload = self._json(url, f"https://www.bilibili.com/video/av{aid}")
        reps = ((payload.get("data") or {}).get("replies") or [])
        out: list[str] = []
        for r in reps:
            msg = str((r.get("content") or {}).get("message") or "").strip()
            if msg:
                out.append(msg.replace("\n", " ")[:90])
            if len(out) >= n:
                break
        return out

    def add_comment(self, oid: int | str, message: str) -> dict:
        """在视频下发一条评论。oid 是 aid（不是 bvid）。

        要登录态：SESSDATA + bili_jct（B 站的 csrf 校验）。B 站对评论有审核和频率
        限制，失败这里统一抛 BiliError，调用方自己决定要不要让用户知道。
        """
        if not self.csrf:
            raise BiliError("没配 bili_jct，发不了评论（B 站要 csrf 校验）")
        message = (message or "").strip()
        if not message:
            raise BiliError("评论内容是空的")
        if len(message) > 1000:
            message = message[:1000]
        body = urllib.parse.urlencode({
            "oid": str(oid),
            "type": "1",
            "message": message,
            "plat": "1",
            "ordering": "heat",
            "csrf": self.csrf,
        }).encode()
        req = urllib.request.Request(
            "https://api.bilibili.com/x/v2/reply/add",
            data=body,
            headers={
                "User-Agent": _UA,
                "Referer": f"https://www.bilibili.com/video/av{oid}",
                "Origin": "https://www.bilibili.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            raise BiliError(f"评论请求失败: {e}") from e
        code = payload.get("code")
        if code != 0:
            raise BiliError(f"评论被拒: {payload.get('message')} (code={code})")
        return payload.get("data") or {}

    def download(self, url: str, dest: str, referer: str = "https://www.bilibili.com/") -> str:
        data = self._raw(url, referer=referer, binary=True)
        with open(dest, "wb") as f:
            f.write(data)
        return dest
