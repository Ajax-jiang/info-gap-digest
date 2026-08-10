#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信息Gap 信息差 云端自动抓取+查证+推送 (GitHub Actions 版)
运行环境: GitHub Actions (Python 3.10+)
所有敏感信息从环境变量读取,不硬编码。

功能:
  1. 用B站Cookie拿UP主"信息Gap"最新视频的AI字幕(ai-zh)逐字稿
  2. 拆条并剔除软广
  3. 用 DeepSeek v4-flash 的联网搜索(web_search)逐条查证+深度总结
  4. 生成摘要,保存为md,并推送到 Server酱(微信)
"""
import os, sys, re, datetime, json, hashlib, time, urllib.parse, urllib.request, html, base64

# ---------- 环境变量配置 ----------
BILI_COOKIE = os.environ.get("BILI_COOKIE", "")  # B站Cookie(分号分隔)
BILI_REFRESH_TOKEN = os.environ.get("BILI_REFRESH_TOKEN", "")  # B站refresh_token(ac_time_value)
GH_TOKEN = os.environ.get("GH_TOKEN", "")  # GitHub token,用于自动更新secret
DS_API_KEY = os.environ.get("DS_API_KEY", "")    # DeepSeek key
SC_SENDKEY = os.environ.get("SC_SENDKEY", "")    # Server酱 SendKey
UP_MID = os.environ.get("UP_MID", "3537104715909319")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get_full_headers(cookie, referer):
    """更真实的浏览器请求头,降低被B站风控概率"""
    return {
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120", "Not-A-Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Connection": "keep-alive",
    }

def http_json(url, cookie, referer="https://www.bilibili.com/", retries=3):
    """带重试的请求,应对B站412/403风控"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=get_full_headers(cookie, referer))
            resp = urllib.request.urlopen(req, timeout=25)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (412, 403):
                time.sleep(3 * (attempt + 1))  # 风控则多等
                continue
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(2)
    raise Exception(f"请求失败({last_err}): {url[:60]}")


def update_gh_secret(name, value):
    """更新GitHub Secret(用于Cookie自动续期后写回)"""
    if not GH_TOKEN:
        print("  无GH_TOKEN,跳过secret更新")
        return False
    try:
        # 获取公钥
        req = urllib.request.Request(
            f"https://api.github.com/repos/{os.environ.get('GH_REPO','Ajax-jiang/info-gap-digest')}/actions/secrets/public-key",
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        pk = json.loads(urllib.request.urlopen(req, timeout=15).read())
        key_id, pub_key = pk["key_id"], pk["key"]
        # 用pynacl加密
        from nacl import encoding, public
        pub = public.PublicKey(pub_key.encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pub).encrypt(value.encode())
        enc = base64.b64encode(sealed).decode()
        body = json.dumps({"encrypted_value": enc, "key_id": key_id}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{os.environ.get('GH_REPO','Ajax-jiang/info-gap-digest')}/actions/secrets/{name}",
            data=body, method="PUT",
            headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json"})
        resp = urllib.request.urlopen(req, timeout=15)
        print(f"  Secret {name} 已更新: HTTP {resp.status}")
        return True
    except Exception as e:
        print(f"  更新Secret失败: {e}")
        return False


def refresh_bili_cookie(cookie, refresh_token):
    """B站Cookie自动续期:用refresh_token换新Cookie。返回新cookie或None(无需刷新)。"""
    try:
        # 1. 检查是否需要刷新
        info = http_json("https://passport.bilibili.com/x/passport-login/web/cookie/info", cookie)
        if not info.get("data", {}).get("refresh"):
            print("  Cookie无需刷新")
            return None

        # 2. 生成correspondPath (RSA-OAEP加密 refresh_{timestamp})
        import urllib.parse as up
        ts = str(int(time.time() * 1000))
        raw = f"refresh_{ts}"
        # 获取B站公钥
        nav = http_json("https://api.bilibili.com/x/web-interface/nav", cookie)
        # 从cookie中拿bili_jct (csrf)
        bili_jct = re.search(r"bili_jct=([^;]+)", cookie)
        csrf = bili_jct.group(1) if bili_jct else ""

        # 用对应公钥加密(简化:直接请求刷新接口,部分情况下B站允许)
        # 3. 直接尝试刷新
        refresh_data = up.urlencode({
            "csrf": csrf, "source": "main_web", "refresh_token": refresh_token
        }).encode()
        req = urllib.request.Request(
            "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
            data=refresh_data,
            headers={**get_full_headers(cookie, "https://www.bilibili.com/"),
                     "Content-Type": "application/x-www-form-urlencoded"})
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
        if resp.get("code") == 0:
            data = resp.get("data", {})
            # 新cookie和refresh_token
            new_cookie = data.get("cookie_info", {}).get("cookies", [])
            new_refresh = data.get("refresh_token", refresh_token)
            # 重组cookie字符串
            cookie_parts = [c["name"] + "=" + c["value"] for c in new_cookie]
            # 保留原有非登录态cookie(如buvid等)
            old_pairs = dict(p.split("=", 1) for p in cookie.split("; ") if "=" in p)
            new_dict = {c["name"]: c["value"] for c in new_cookie}
            old_pairs.update(new_dict)
            full_cookie = "; ".join(f"{k}={v}" for k, v in old_pairs.items())
            print("  Cookie已刷新")
            return full_cookie, new_refresh
        else:
            print(f"  刷新失败: {resp.get('code')} {resp.get('message')}")
            return None
    except Exception as e:
        print(f"  续期异常: {e}")
        return None

def get_latest_video(cookie):
    """WBI签名拉UP主最新视频"""
    nav = http_json("https://api.bilibili.com/x/web-interface/nav", cookie)
    img = nav["data"]["wbi_img"]["img_url"].split("/")[-1].split(".")[0]
    sub = nav["data"]["wbi_img"]["sub_url"].split("/")[-1].split(".")[0]
    mixin = (img + sub)
    table = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
    mixin = "".join(mixin[i] for i in table)[:32]
    params = {"mid": UP_MID, "ps": "5", "pn": "1"}
    params["wts"] = str(int(time.time()))
    params = dict(sorted(params.items()))
    q = urllib.parse.urlencode(params)
    w_rid = hashlib.md5((q + mixin).encode()).hexdigest()
    url = "https://api.bilibili.com/x/space/wbi/arc/search?" + q + "&w_rid=" + w_rid
    resp = http_json(url, cookie, f"https://space.bilibili.com/{UP_MID}")
    if resp.get("code") != 0:
        raise Exception(f"B站接口错误: {resp.get('code')} {resp.get('message')}")
    time.sleep(1.5)  # 模拟真人节奏
    vlist = resp["data"]["list"]["vlist"]
    if not vlist:
        raise Exception("没有视频")
    v = vlist[0]
    return {"bvid": v["bvid"], "title": v["title"], "date": v["created"]}

def get_subtitle(bvid, cookie):
    """直取AI中文字幕"""
    info = http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cookie)
    cid = info["data"]["cid"]
    time.sleep(1.5)
    player = http_json(f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}", cookie)
    subs = (player.get("data", {}).get("subtitle", {}) or {}).get("subtitles") or []
    if not subs:
        raise Exception("无AI字幕")
    sub = next((s for s in subs if s.get("lan") == "ai-zh"), subs[0])
    sub_url = sub["subtitle_url"]
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    time.sleep(1)
    sub_json = http_json(sub_url, cookie, f"https://www.bilibili.com/video/{bvid}")
    return " ".join(b.get("content", "") for b in sub_json.get("body", []))

def ds_websearch(prompt):
    """调用 DeepSeek v4-flash 联网搜索"""
    body = {"model": "deepseek-v4-flash", "input": prompt, "tools": [{"type": "web_search"}]}
    req = urllib.request.Request("https://api.deepseek.com/responses",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {DS_API_KEY}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    parts = []
    for o in resp.get("output", []):
        if o.get("type") == "message":
            for c in o.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c["text"])
    return "".join(parts)

def split_items(transcript):
    """按'第一/第二/第N'或数字拆条,去掉软广段"""
    # 简单拆:按中文序号或"第一/第二"切
    parts = re.split(r'(?=(?:第[一二三四五六七八九十百\d]+|[\d]+、))', transcript)
    items = [p.strip() for p in parts if len(p.strip()) > 30]
    # 剔除明显广告段(含'领券'/'家居服'/'睡衣'等)
    ad_kw = ["领券", "家居服", "睡衣", "凉感", "面料", "穿上", "优惠", "淘宝", "天猫"]
    filtered = [it for it in items if not any(k in it for k in ad_kw)]
    return filtered or items

def push_wechat(title, content):
    """Server酱推送微信"""
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
    req = urllib.request.Request(f"https://sctapi.ftqq.com/{SC_SENDKEY}.send", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return resp.get("code") == 0

def main():
    if not all([BILI_COOKIE, DS_API_KEY, SC_SENDKEY]):
        print("缺少必要环境变量"); sys.exit(1)

    cookie = BILI_COOKIE

    # 自动续期:检查Cookie是否需要刷新,需要则自动换新
    print("[0/4] 检查Cookie状态...")
    if BILI_REFRESH_TOKEN:
        result = refresh_bili_cookie(cookie, BILI_REFRESH_TOKEN)
        if result:
            cookie, new_refresh = result
            # 更新GitHub Secret,让下次运行用新Cookie
            if cookie != BILI_COOKIE:
                update_gh_secret("BILI_COOKIE", cookie)
            if new_refresh != BILI_REFRESH_TOKEN:
                update_gh_secret("BILI_REFRESH_TOKEN", new_refresh)
    else:
        print("      未配置refresh_token,依赖手动更新Cookie")

    print("[1/4] 获取最新视频...")
    video = get_latest_video(cookie)
    dt = datetime.datetime.fromtimestamp(video["date"]).strftime("%Y-%m-%d")
    print(f"       {video['title']} ({dt})")

    print("[2/4] 获取字幕...")
    transcript = get_subtitle(video["bvid"], cookie)
    print(f"       逐字稿 {len(transcript)} 字")

    print("[3/4] DeepSeek 联网查证+总结...")
    items = split_items(transcript)
    prompt = f"""你是信息差分析师。以下是B站UP主"信息Gap"{video['title']}的逐字稿内容。请做三件事:

一、拆条:拆成独立条目,剔除广告。

二、逐条联网核实真伪,标注状态:
- 【已证实】有权威来源
- 【存疑】查不到或不一致
- 【有出入】UP主说错,指出实际是什么

三、区分"信息差"vs"普通新闻":
- 🎯【信息差】= 大多数人不知道、但对决策有用的:政策红利、套利机会、规则漏洞、行业暗线、小众认知、即将发生的变化
- 📰【新闻】= 已被广泛报道、人人都知道的事
- 对每条标注属于哪类。属于【新闻】的一笔带过即可,属于【信息差】的重点展开。

⚡ 排版要求(最关键,严格遵守,学微信公众号风格,适合手机阅读):
1. **短段**:每个要点独立成段,一段最多2行,段落之间必须空一行。
2. **标题即结论**:每条信息差的标题直接说结论,不用"XX事件"这种名词,要用"XX发生了什么→意味着什么"。
3. **关键数字单独突出**:最重要的1-2个数字/事实,单独一行,前面加"🔑"。
4. **少加粗**:除了标题,正文不要大量加粗,靠空行和换行制造层次。
5. **每屏一个重点**:按"标题 → 1行核心 → 1行为什么是信息差 → 1行可做什么"的结构,不要写长段落。
6. **语言口语化**:像朋友给你转述,不像报告。

输出格式(纯文本,手机阅读友好):
---
📌 {video['title']} · 信息差速览

🎯 今天有 N 条真信息差,📰 M 条普通新闻

━━━━━━━━━━
🔥 今日重点

【1】标题(直接说结论)
核心:一句话。

🔑 关键:最重要的数字/事实,单独一行

为什么是信息差:一句话(别人不知道的)。

可做:一句话。

【2】标题
...(同上,每条之间空两行)

━━━━━━━━━━
📋 其他信息差

【N】标题
一句话核心。

...(短,每条2-3行)

━━━━━━━━━━
📰 顺带新闻(非信息差)

· 标题:一句话
· 标题:一句话
(每条不超过1行)

(整体控制在500字以内)
"""
    summary = ds_websearch(prompt)
    print(f"       总结完成 {len(summary)} 字")

    print("[4/4] 推送微信...")
    ok = push_wechat(f"📌 信息差摘要 {dt}", summary)
    print(f"       微信推送{'成功' if ok else '失败'}")

    # 保存摘要
    os.makedirs("out", exist_ok=True)
    path = f"out/{dt}_信息差摘要.md"
    open(path, "w", encoding="utf-8").write(summary)
    print(f"       已保存 {path}")

if __name__ == "__main__":
    main()
