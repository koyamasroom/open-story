#!/usr/bin/env python3
"""バルダイニングイッツ OPEN告知ストーリーの自動投稿。

- 今日の曜日のフォルダ（画像/月 など）から、いちばん長く流していない画像を選ぶ
- 開店時刻まで待ってから、Instagram 公式API でストーリーに流す
- 流した記録は state/posted.json に残す

動き方（環境変数 POST_MODE）:
  dry   … テスト。どの画像を流すか表示するだけ。Instagram には一切つながない（初期設定）
  check … 確認。Instagram につないで、アカウントと画像を確かめる。投稿はしない
  live  … 本番。ストーリーに流す
"""
import argparse
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(os.environ.get("OPEN_STORY_ROOT", Path(__file__).resolve().parent))
IMAGE_DIR = ROOT / "画像"
STATE_FILE = ROOT / "state" / "posted.json"

# datetime.weekday(): 0=月 … 6=日
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
OPEN_TIME = {"月": (17, 0), "火": (17, 0), "水": (17, 0), "木": (17, 0),
             "金": (17, 0), "土": (15, 0), "日": (15, 0)}

# 間違ったアカウントに流さないための確認
EXPECTED_USERNAME = os.environ.get("IG_EXPECTED_USERNAME", "eatit_0601")
API_BASE = "https://graph.instagram.com/" + os.environ.get("IG_API_VERSION", "v27.0")

MAX_WAIT_MINUTES = 75     # 開店のこれより前に起動したら、待たずに終わる
MAX_LATE_MINUTES = 180    # 開店からこれ以上遅れたら、流さない
MODE_NAMES = {"dry": "テスト", "check": "確認", "live": "本番"}


class StoryError(Exception):
    pass


# ---------- 記録 ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posts": [], "last_posted": {}}


def save_state(state):
    state["posts"] = state["posts"][-200:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


# ---------- 画像選び ----------
def nfc(s):
    return unicodedata.normalize("NFC", s)


def pick_image(day, state):
    folder = IMAGE_DIR / day
    files = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.startswith("."))
    jpegs = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg")]
    for p in files:
        if p not in jpegs:
            print(f"⚠️ {day}/{p.name} はJPEGではないので使いません（InstagramはJPEGだけ受け付けます）")
    if not jpegs:
        raise StoryError(f"「画像/{day}」に使えるJPEG画像がありません。今日は流しません。")
    last = state.get("last_posted", {})

    def key(p):
        rel = nfc(f"{day}/{p.name}")
        return (last.get(rel, ""), nfc(p.name))  # 流したことがない画像（""）が先

    return sorted(jpegs, key=key)[0]


def public_url(day, image):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise StoryError("GITHUB_REPOSITORY がありません（GitHubの上で動かしてください）")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    path = urllib.parse.quote(nfc(f"画像/{day}/{image.name}"))
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


# ---------- Instagram API ----------
def api(method, path, token, **params):
    params["access_token"] = token
    data = urllib.parse.urlencode(params)
    url = f"{API_BASE}/{path}"
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data}")
    else:
        req = urllib.request.Request(url, data=data.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").replace(token, "***")
        raise StoryError(f"Instagramからエラーが返りました（{e.code}）: {body}") from None


def check_account(token):
    me = api("GET", "me", token, fields="user_id,username")
    if me.get("username") != EXPECTED_USERNAME:
        raise StoryError(f"つながったアカウントが @{me.get('username')} でした。"
                         f"@{EXPECTED_USERNAME} 以外には流さないので止めます。")
    print(f"✅ アカウント確認OK：@{me['username']}")
    return me["user_id"]


def create_container(token, user_id, image_url):
    res = api("POST", f"{user_id}/media", token, image_url=image_url, media_type="STORIES")
    cid = res["id"]
    for _ in range(30):
        st = api("GET", cid, token, fields="status_code").get("status_code")
        if st == "FINISHED":
            print("✅ Instagramが画像を受け取りました")
            return cid
        if st in ("ERROR", "EXPIRED"):
            raise StoryError(f"Instagramが画像を受け取れませんでした（{st}）。画像の形式やサイズを確認してください。")
        time.sleep(5)
    raise StoryError("Instagramの画像の準備が終わりませんでした（時間切れ）")


# ---------- 時間 ----------
def wait_until_open(now, day):
    h, m = OPEN_TIME[day]
    open_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < open_at:
        wait = (open_at - now).total_seconds()
        if wait > MAX_WAIT_MINUTES * 60:
            raise StoryError(f"開店（{h}:{m:02d}）まで{int(wait // 60)}分もあるので、今回は何もしません。")
        print(f"⏳ 開店 {h}:{m:02d} まで {int(wait // 60)}分{int(wait % 60)}秒 待ちます")
        time.sleep(wait)
    elif now > open_at + timedelta(minutes=MAX_LATE_MINUTES):
        raise StoryError(f"開店から{MAX_LATE_MINUTES}分以上すぎているので、今日は流しません。")


# ---------- 本体 ----------
def main():
    ap = argparse.ArgumentParser(description="OPEN告知ストーリーを流す")
    ap.add_argument("--manual", action="store_true",
                    help="手動で動かす（開店時刻を待たない・今日すでに動いていてもやる）")
    ap.add_argument("--day", choices=WEEKDAYS, help="曜日を指定する（テスト用。省略で今日）")
    args = ap.parse_args()

    mode = os.environ.get("POST_MODE", "dry").strip() or "dry"
    if mode not in MODE_NAMES:
        raise StoryError(f"POST_MODE は dry / check / live のどれかにしてください（今は {mode}）")
    now = datetime.now(JST)
    today = now.date().isoformat()
    day = args.day or WEEKDAYS[now.weekday()]
    print(f"📅 {today}（{day}）／ モード：{MODE_NAMES[mode]}")

    state = load_state()
    if not args.manual and any(p["date"] == today for p in state["posts"]):
        print("今日はもう動いているので、何もしません（予備の起動）")
        return

    image = pick_image(day, state)
    rel = nfc(f"{day}/{image.name}")
    print(f"🖼 今日の画像：画像/{rel}")

    if not args.manual:
        wait_until_open(datetime.now(JST), day)

    media_id = None
    if mode == "dry":
        print("（テストなので、Instagramにはつないでいません）")
    else:
        token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
        if not token:
            raise StoryError("合い鍵（IG_ACCESS_TOKEN）が設定されていません")
        url = public_url(day, image)
        user_id = check_account(token)
        cid = create_container(token, user_id, url)
        if mode == "check":
            print("（確認なので、ストーリーには流していません）")
        else:
            media_id = api("POST", f"{user_id}/media_publish", token, creation_id=cid)["id"]
            print(f"🎉 ストーリーに流しました！（投稿ID {media_id}）")

    state["posts"].append({"date": today, "day": day, "image": rel,
                           "mode": MODE_NAMES[mode], "media_id": media_id,
                           "time": datetime.now(JST).strftime("%H:%M")})
    if mode != "check":  # テストでも順番が回る様子を見られるように記録する
        state["last_posted"][rel] = today
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except StoryError as e:
        print(f"❌ {e}")
        sys.exit(1)
