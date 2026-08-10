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
import os, sys, re, datetime, json, hashlib, time, urllib.parse, urllib.request, html

# ---------- 环境变量配置 ----------
BILI_COOKIE = os.environ.get("BILI_COOKIE", "")  # B站Cookie(分号分隔)
DS_API_KEY = os.environ.get("DS_API_KEY", "")    # DeepSeek key
SC_SENDKEY = os.environ.get("SC_SENDKEY", "")    # Server酱 SendKey
UP_MID = os.environ.get("UP_MID", "3537104715909319")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"

def http_json(url, cookie, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": cookie, "Referer": referer})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

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
    vlist = resp["data"]["list"]["vlist"]
    if not vlist:
        raise Exception("没有视频")
    v = vlist[0]
    return {"bvid": v["bvid"], "title": v["title"], "date": v["created"]}

def get_subtitle(bvid, cookie):
    """直取AI中文字幕"""
    info = http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cookie)
    cid = info["data"]["cid"]
    player = http_json(f"https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}", cookie)
    subs = (player.get("data", {}).get("subtitle", {}) or {}).get("subtitles") or []
    if not subs:
        raise Exception("无AI字幕")
    sub = next((s for s in subs if s.get("lan") == "ai-zh"), subs[0])
    sub_url = sub["subtitle_url"]
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
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

    print("[1/4] 获取最新视频...")
    video = get_latest_video(BILI_COOKIE)
    dt = datetime.datetime.fromtimestamp(video["date"]).strftime("%Y-%m-%d")
    print(f"       {video['title']} ({dt})")

    print("[2/4] 获取字幕...")
    transcript = get_subtitle(video["bvid"], BILI_COOKIE)
    print(f"       逐字稿 {len(transcript)} 字")

    print("[3/4] DeepSeek 联网查证+总结...")
    items = split_items(transcript)
    prompt = f"""你是信息差核查助手。以下是B站UP主"信息Gap"{video['title']}的逐字稿内容,请:
1. 拆分成独立的信息差条目
2. 对每条联网核实真伪,标注【已证实】/【存疑】/【与事实有出入】
3. 深度补充(背景+关键数据+影响+多方视角)
4. 剔除广告内容

逐字稿:
{transcript[:8000]}

请输出结构化摘要,格式:
**N.【状态】标题**
- 核心内容
- 深度:背景+数据+影响+视角
- 核实:依据"""
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
