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
    """调用 DeepSeek v4-flash 联网搜索(超时180秒,处理深度分析+JSON输出)"""
    body = {"model": "deepseek-v4-flash", "input": prompt, "tools": [{"type": "web_search"}]}
    req = urllib.request.Request("https://api.deepseek.com/responses",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {DS_API_KEY}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
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


def _esc(s):
    """HTML转义"""
    if s is None: return ""
    return html.escape(str(s)).replace('\n', '<br>')

STATUS_LABEL = {"confirmed": "✅已证实", "doubtful": "⚠️存疑", "discrepancy": "⚠️有出入"}
STATUS_COLOR = {"confirmed": "#0a7d3e", "doubtful": "#b26a00", "discrepancy": "#b3261e"}

def render_html(data, video, dt):
    """把JSON数据渲染成精品HTML页面(30秒版+可展开408深度)"""
    items = data.get("items", [])
    info_items = [it for it in items if it.get("category") == "info"]
    brief_items = [it for it in items if it.get("category") == "brief"]
    news_items = [it for it in items if it.get("category") == "news"]

    def status_badge(it):
        st = it.get("status", "confirmed")
        label = STATUS_LABEL.get(st, st)
        color = STATUS_COLOR.get(st, "#666")
        note = _esc(it.get("status_note", ""))
        return f'<span class="badge" style="background:{color}">{label}</span>' + (f'<span class="note">{note}</span>' if note else '')

    def info_card(it):
        fields = []
        if it.get("what"): fields.append(("这是什么", it["what"]))
        if it.get("background"): fields.append(("来龙去脉", it["background"]))
        if it.get("mechanism"): fields.append(("底层机制", it["mechanism"]))
        if it.get("impact"): fields.append(("影响", it["impact"]))
        if it.get("pitfall"): fields.append(("坑与误区", it["pitfall"]))
        if it.get("analogy"): fields.append(("类比", it["analogy"]))
        keys = "".join(f'<div class="keynum">🔑 {_esc(k)}</div>' for k in it.get("key_numbers", []))
        takeaway = f'<div class="takeaway">💡 {_esc(it.get("takeaway",""))}</div>' if it.get("takeaway") else ""
        body = "".join(
            f'<div class="dfield"><span class="dlabel">{lab}</span><div class="dtext">{_esc(val)}</div></div>'
            for lab, val in fields
        )
        # 见微知著:前瞻推演区
        foresight_html = ""
        if it.get("foresight") or it.get("evidence") or it.get("confidence") or it.get("counter") or it.get("action"):
            foresight_html = f'''<div class="foresight">
  <div class="fs-title">🔮 见微知著 · 前瞻推演</div>
  {f'<div class="fs-trend">趋势: {_esc(it["foresight"])}</div>' if it.get("foresight") else ''}
  <div class="fs-meta">
    {f'<span class="fs-chip">🧭 证据强度:{_esc(it["evidence"])}</span>' if it.get("evidence") else ''}
    {f'<span class="fs-chip">🎯 置信度:{_esc(it["confidence"])}</span>' if it.get("confidence") else ''}
  </div>
  {f'<div class="fs-counter">⚠️ 反证:{_esc(it["counter"])}</div>' if it.get("counter") else ''}
  {f'<div class="fs-action">✅ 现在可做:{_esc(it["action"])}</div>' if it.get("action") else ''}
</div>'''
        snippet = ""
        if it.get("foresight"):
            snippet = f'<div class="fs-snippet">🔮 {_esc(it["foresight"])[:50]}…</div>'
        elif it.get("what"):
            snippet = f'<div class="card-snippet">{_esc(it["what"])[:40]}…</div>'
        return f'''<div class="card info">
  <div class="card-head">
    <span class="card-title">{_esc(it.get("title",""))}</span>
    {status_badge(it)}
  </div>
  {snippet}
  <details>
    <summary>深度解读</summary>
    {foresight_html}
    {keys}
    {body}
    {takeaway}
  </details>
</div>'''

    def simple_card(it):
        return f'''<div class="card brief">
  <div class="card-head"><span class="card-title">{_esc(it.get("title",""))}</span>{status_badge(it)}</div>
  <div class="card-body">{_esc(it.get("what",""))}{_esc(it.get("impact",""))}</div>
</div>'''

    def news_card(it):
        return f'''<div class="card news"><div class="card-head"><span class="card-title">{_esc(it.get("title",""))}</span>{status_badge(it)}</div></div>'''

    info_html = "".join(info_card(it) for it in info_items) or "<p style='color:#888'>今日无深度信息差</p>"
    brief_html = "".join(simple_card(it) for it in brief_items)
    news_html = "".join(news_card(it) for it in news_items)

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(dt)} 信息差速览</title>
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" href="icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="信息差">
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #222; padding-bottom: 40px; }}
.header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: #fff; padding: 28px 20px 24px; }}
.header h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 6px; }}
.header .date {{ font-size: 13px; opacity: .8; }}
.header .summary {{ margin-top: 12px; font-size: 14px; background: rgba(255,255,255,.12); padding: 10px 14px; border-radius: 10px; line-height: 1.5; }}
.container {{ max-width: 640px; margin: 0 auto; padding: 16px 14px; }}
.section-title {{ font-size: 16px; font-weight: 700; margin: 18px 0 10px; display: flex; align-items: center; gap: 6px; }}
.card {{ background: #fff; border-radius: 14px; padding: 14px 16px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
.card-head {{ display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }}
.card-title {{ font-size: 15px; font-weight: 600; flex: 1; line-height: 1.4; }}
.badge {{ font-size: 11px; color: #fff; padding: 2px 8px; border-radius: 20px; white-space: nowrap; margin-top: 2px; }}
.note {{ font-size: 11px; color: #999; display: block; margin-top: 4px; }}
.card-snippet {{ font-size: 13px; color: #666; line-height: 1.5; }}
.card-body {{ font-size: 13px; color: #555; line-height: 1.5; }}
details {{ margin-top: 8px; }}
summary {{ cursor: pointer; font-size: 13px; color: #0a5ad1; font-weight: 600; padding: 6px 0; }}
.dfield {{ margin-bottom: 8px; }}
.dlabel {{ font-size: 12px; font-weight: 700; color: #0a5ad1; display: block; margin-bottom: 2px; }}
.dtext {{ font-size: 13px; color: #333; line-height: 1.6; }}
.keynum {{ font-size: 14px; font-weight: 700; color: #b26a00; background: #fff8e1; padding: 6px 10px; border-radius: 8px; margin-bottom: 8px; }}
.takeaway {{ font-size: 13px; font-weight: 600; color: #0a7d3e; background: #e8f5e9; padding: 8px 10px; border-radius: 8px; margin-top: 6px; }}
.fs-snippet {{ font-size: 13px; color: #0a5ad1; background: #eef3ff; padding: 8px 10px; border-radius: 8px; margin-top: 6px; line-height: 1.5; }}
.foresight {{ background: linear-gradient(135deg, #f6f0ff, #eef3ff); border: 1px solid #d6c8f5; border-radius: 12px; padding: 12px; margin: 8px 0; }}
.fs-title {{ font-size: 14px; font-weight: 700; color: #5b3fa8; margin-bottom: 8px; }}
.fs-trend {{ font-size: 13px; color: #333; line-height: 1.6; margin-bottom: 6px; }}
.fs-meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }}
.fs-chip {{ font-size: 12px; background: #fff; border: 1px solid #ddd; color: #555; padding: 3px 8px; border-radius: 20px; }}
.fs-counter {{ font-size: 12px; color: #b3261e; line-height: 1.5; margin-top: 4px; }}
.fs-action {{ font-size: 13px; font-weight: 600; color: #0a7d3e; background: #e8f5e9; padding: 6px 10px; border-radius: 8px; margin-top: 6px; line-height: 1.5; }}
.card.news {{ background: #f9f9fa; }}
.card.news .card-title {{ color: #888; font-weight: 500; }}
.verify {{ background: #eef3ff; border-radius: 14px; padding: 14px 16px; font-size: 13px; color: #345; line-height: 1.6; margin-top: 8px; }}
.footer {{ text-align: center; font-size: 11px; color: #aaa; margin-top: 24px; }}
</style>
</head>
<body>
<div class="header">
  <div class="date">信息Gap · {_esc(dt)}</div>
  <h1>📌 信息差速览</h1>
  <div class="summary">👀 {_esc(data.get("summary_line",""))}</div>
</div>
<div class="container">
  <div class="section-title">🔥 必看（深度解读）</div>
  {info_html}
  <div class="section-title">📋 其他信息差</div>
  {brief_html or "<p style='color:#999;font-size:13px'>今日无</p>"}
  <div class="section-title">📰 顺带新闻</div>
  {news_html or "<p style='color:#999;font-size:13px'>今日无</p>"}
  <div class="verify">✅ {_esc(data.get("verify_summary",""))}</div>
  <div class="footer">信息Gap · 信息差速览 · 每日自动生成</div>
</div>
</body>
</html>'''
    return html_doc


def gh_put_file(path, content_bytes, message):
    """通用:上传/更新仓库文件"""
    repo = os.environ.get("GH_REPO", "Ajax-jiang/info-gap-digest")
    sha = ""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}",
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        resp = urllib.request.urlopen(req, timeout=15).read()
        sha = json.loads(resp).get("sha", "")
    except Exception:
        pass
    body = {"message": message, "content": base64.b64encode(content_bytes).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}",
        data=json.dumps(body).encode(), method="PUT",
        headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json",
                 "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:
        print(f"  上传 {path} 失败: {e}")
        return False


def upload_to_pages(html_content, dt):
    """上传daily-{date}.html + 更新index.html(自动跳转到最新),返回页面URL"""
    if not GH_TOKEN:
        return ""
    repo = os.environ.get("GH_REPO", "Ajax-jiang/info-gap-digest")
    # 1. 上传带日期的内容页 daily-2026-08-11.html
    daily_path = f"daily-{dt}.html"
    ok1 = gh_put_file(daily_path, html_content.encode(), f"每日更新: {dt}")
    # 2. 生成自动跳转的index.html(指向最新daily)
    redirect = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="0; url={daily_path}">
<link rel="apple-touch-icon" href="icon.png">
<title>信息差速览</title>
</head>
<body>
<p>正在加载最新信息差… <a href="{daily_path}">点击进入</a></p>
</body>
</html>'''
    ok2 = gh_put_file("index.html", redirect.encode(), f"更新入口 → {daily_path}")
    if ok1 and ok2:
        return f"https://ajax-jiang.github.io/{repo.split('/')[1]}/"
    return ""


def upload_icon():
    """上传icon.png到GitHub仓库(供apple-touch-icon使用)"""
    if not GH_TOKEN:
        return
    repo = os.environ.get("GH_REPO", "Ajax-jiang/info-gap-digest")
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/icon.png",
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        try:
            resp = urllib.request.urlopen(req, timeout=15).read()
            sha = json.loads(resp).get("sha", "")
        except Exception:
            sha = ""
        icon_data = open("out/icon.png", "rb").read()
        body = {"message": "更新图标", "content": base64.b64encode(icon_data).decode()}
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/icon.png",
            data=json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=20)
        print("       icon.png 已上传")
    except Exception as e:
        print(f"  图标上传失败: {e}")

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

    print("[3/4] DeepSeek 结构化分析...")
    items = split_items(transcript)
    prompt = f"""你是资深的信息差分析师。以下是B站UP主"信息Gap"{video['title']}的逐字稿,请把它拆成条目并输出**严格的JSON**(不要任何其他文字,不要markdown围栏)。

逐字稿:
{transcript[:8000]}

规则:
1. 拆出独立条目,剔除广告(如家居服等)。
2. 对每条信息,用你的知识判断真伪,status填:
   - "confirmed"(可信/有依据) / "doubtful"(存疑) / "discrepancy"(你确定UP主说错了,注明status_note纠正)
3. category分类:
   - "info"= 真信息差(政策红利/套利机会/规则漏洞/行业暗线/小众认知/即将发生的变化)
   - "brief"= 次要信息差(值得提,不用深度)
   - "news"= 普通新闻(人人皆知)
4. 对 category="info" 的条目,用"见微知著"框架填这些字段(这是最重要的部分,学扁鹊大哥:从早期信号推演趋势):
   - what: 这是什么(一句,外行秒懂)
   - background: 来龙去脉(2句)
   - mechanism: 底层机制(2句)
   - impact: 影响(正面+反面,2句)
   - pitfall: 坑与误区(1-2句)
   - analogy: 生活化类比(1句)
   - key_numbers: 关键数字数组(每条一句含数字)
   - takeaway: 一句话"该注意什么/可做什么"
   - **foresight(前瞻推演,见微知著,新增,最重要)**: 这个信号背后暴露了什么趋势?如果持续,1-3年后会发生什么?要给出"时间窗+趋势判断"。格式:"🔮 趋势:【时间窗】+判断"
   - **evidence(证据强度)**: 填 "强"/"中"/"弱" + 一句话说明证据(如"人口数据是硬趋势"或"目前只是个案")
   - **confidence(置信度)**: 填 "高"/"中"/"低" + 一句话(方向是否确定?具体到哪家公司/机构是否确定?)
   - **counter(反证/如果错了)**: 这个判断可能错在哪?什么情况下不成立?(1句)
   - **action(现在可做什么)**: 针对不同角色(普通人/从业者/学生/投资者)的1-2条可执行建议
   注意:这些字段用你的知识写,不准的不要编。预判只给"倾向和概率",绝不编造具体事件。
5. category="brief" 只填:title/category/status/what(一句)/impact(一句)。
6. category="news" 只填:title/category/status。

输出JSON结构:
{{
  "date": "{video['title']}",
  "summary_line": "一句话总览(15字内)",
  "items": [
    {{"title":"...", "category":"info", "status":"confirmed", "status_note":"...", "what":"...", "background":"...", "mechanism":"...", "impact":"...", "pitfall":"...", "analogy":"...", "key_numbers":["..."], "takeaway":"...", "foresight":"...", "evidence":"...", "confidence":"...", "counter":"...", "action":"..."}}
  ],
  "verify_summary": "核验汇总(共N条,已证实X,存疑Y,有出入Z)"
}}
JSON必须合法,能被json.loads解析。"""
    raw_output = ds_websearch(prompt)
    print(f"       分析完成,尝试解析JSON...")
    # 提取JSON(去掉可能的围栏或前后杂讯)
    import json as _json
    data = None
    try:
        start = raw_output.find('{')
        end = raw_output.rfind('}')
        if start >= 0 and end > start:
            json_str = raw_output[start:end+1]
            data = _json.loads(json_str)
            print(f"       JSON解析成功,{len(data.get('items',[]))} 条")
    except Exception as e:
        print(f"       JSON解析失败({e}),回退为纯文本")

    # 保存原始分析
    os.makedirs("out", exist_ok=True)
    path = f"out/{dt}_信息差分析.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw_output if data is None else _json.dumps(data, ensure_ascii=False, indent=2))
    print(f"       已保存分析 {path}")

    # 渲染精品HTML(如果JSON解析成功)
    html_url = ""
    if data:
        try:
            html_content = render_html(data, video, dt)
            html_path = f"out/index.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"       HTML已生成 {len(html_content)} 字符")
            # 上传到GitHub Pages
            html_url = upload_to_pages(html_content, dt)
            print(f"       页面地址: {html_url}")
        except Exception as e:
            print(f"       HTML渲染/上传失败: {e}")

    # 生成一个简单的图标(如需定制可替换)
    try:
        import struct, zlib
        # 生成一个 180x180 的图标(深色底+📰)
        # 简化:生成纯色圆角图标
        try:
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (180, 180), (26, 26, 46))
            draw = ImageDraw.Draw(img)
            # 画个圆角方块
            draw.rounded_rectangle([4, 4, 176, 176], radius=40, fill=(22, 33, 62))
            draw.text((90, 90), '📰', anchor='mm', font_size=60)
            img.save('out/icon.png')
            print("       图标已生成 out/icon.png")
            # 上传icon.png
            upload_icon()
        except ImportError:
            print("       无PIL,跳过图标生成")
    except Exception as e:
        print(f"       图标生成失败: {e}")

    print("[4/4] 完成(不推送微信,用户直接访问页面)")
    if data and html_url:
        print(f"       今日页面已更新: {html_url}")
    elif data:
        print("       页面已生成(未上传,可能缺GH_TOKEN)")

if __name__ == "__main__":
    main()
