#!/usr/bin/env python3
"""OPEN告知ストーリーの自動投稿（バルダイニングイッツ／チーナテリアハナヤ）。

- お店（--store）の今日の曜日のフォルダから、いちばん長く流していない画像・動画を選ぶ
  （画像は JPEG、動画は MP4 / MOV）
- 開店時刻まで待ってから、Instagram 公式API でストーリーに流す
- 流した記録は state/ に残す（お店ごとに別ファイル）

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
# datetime.weekday(): 0=月 … 6=日
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# お店ごとの設定。open_time に無い曜日は定休日（流さない）
STORES = {
    "its": {
        "name": "バルダイニングイッツ",
        "folder": "画像",
        "state": "posted.json",
        "username": "eatit_0601",          # 間違ったアカウントに流さないための確認
        "token_env": "IG_ACCESS_TOKEN",
        "open_time": {"月": (17, 0), "火": (17, 0), "水": (17, 0), "木": (17, 0),
                      "金": (17, 0), "土": (15, 0), "日": (15, 0)},
    },
    "hanaya": {
        "name": "チーナテリアハナヤ",
        "folder": "ハナヤ",
        "state": "posted_hanaya.json",
        "username": "cinateria2021",
        "token_env": "IG_ACCESS_TOKEN_HANAYA",  # 無ければ IG_ACCESS_TOKEN を使う
        "open_time": {d: (11, 0) for d in ["火", "水", "木", "金", "土", "日"]},  # 月曜定休
    },
}
STORE = STORES["its"]  # main() で --store に合わせて入れ替える
# 統合ログイン（システムユーザーの合い鍵・無期限）は graph.facebook.com につなぐ
API_BASE = "https://graph.facebook.com/" + os.environ.get("IG_API_VERSION", "v27.0")

MAX_WAIT_MINUTES = 75     # 開店のこれより前に起動したら、待たずに終わる
MAX_LATE_MINUTES = 180    # 開店からこれ以上遅れたら、流さない
MODE_NAMES = {"dry": "テスト", "check": "確認", "live": "本番"}
IMAGE_EXT = (".jpg", ".jpeg")
VIDEO_EXT = (".mp4", ".mov")
VIDEO_MAX_MB = 100  # Instagram のストーリー動画の上限


class StoryError(Exception):
    pass


# ---------- 記録 ----------
def state_file():
    return ROOT / "state" / STORE["state"]


def load_state():
    if state_file().exists():
        return json.loads(state_file().read_text(encoding="utf-8"))
    return {"posts": [], "last_posted": {}}


def save_state(state):
    state["posts"] = state["posts"][-200:]
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


# ---------- 画像選び ----------
def nfc(s):
    return unicodedata.normalize("NFC", s)


def pick_image(day, state):
    folder = ROOT / STORE["folder"] / day
    files = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.startswith("."))
    usable = []
    for p in files:
        ext = p.suffix.lower()
        if ext in VIDEO_EXT and p.stat().st_size > VIDEO_MAX_MB * 1024 * 1024:
            print(f"⚠️ {day}/{p.name} は{VIDEO_MAX_MB}MBより大きいので使いません")
        elif ext in IMAGE_EXT + VIDEO_EXT:
            usable.append(p)
        else:
            print(f"⚠️ {day}/{p.name} は使えない形式なので飛ばします（画像はJPEG、動画はMP4かMOV）")
    if not usable:
        raise StoryError(f"「{STORE['folder']}/{day}」に使える画像・動画がありません。今日は流しません。")
    last = state.get("last_posted", {})

    def key(p):
        rel = nfc(f"{day}/{p.name}")
        return (last.get(rel, ""), nfc(p.name))  # 流したことがない画像（""）が先

    return sorted(usable, key=key)[0]


def public_url(day, image):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise StoryError("GITHUB_REPOSITORY がありません（GitHubの上で動かしてください）")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    path = urllib.parse.quote(nfc(f"{STORE['folder']}/{day}/{image.name}"))
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
    """合い鍵につながっているInstagramアカウントの中から、お店のアカウントを探す。"""
    res = api("GET", "me/assigned_instagram_accounts", token)
    found = []
    for acc in res.get("data", []):
        ig_id = acc.get("id")
        if not ig_id:
            continue
        name = acc.get("username") or api("GET", ig_id, token, fields="username").get("username")
        found.append(name)
        if name == STORE["username"]:
            print(f"✅ アカウント確認OK：@{name}")
            return ig_id
    raise StoryError(f"合い鍵につながっているアカウントは {found or 'なし'} でした。"
                     f"@{STORE['username']} 以外には流さないので止めます。")


def is_video(path):
    return path.suffix.lower() in VIDEO_EXT


def create_container(token, user_id, media_url, video):
    key = "video_url" if video else "image_url"
    res = api("POST", f"{user_id}/media", token, media_type="STORIES", **{key: media_url})
    cid = res["id"]
    kind = "動画" if video else "画像"
    tries = 60 if video else 30  # 動画は準備に時間がかかる（最大10分待つ）
    for _ in range(tries):
        info = api("GET", cid, token, fields="status_code,status")
        st = info.get("status_code")
        if st == "FINISHED":
            print(f"✅ Instagramが{kind}を受け取りました")
            return cid
        if st in ("ERROR", "EXPIRED"):
            raise StoryError(f"Instagramが{kind}を受け取れませんでした（{st}：{info.get('status', '')}）。"
                             f"形式・長さ・サイズを確認してください。")
        time.sleep(10 if video else 5)
    raise StoryError(f"Instagramの{kind}の準備が終わりませんでした（時間切れ）")


# ---------- 時間 ----------
def wait_until_open(now, day):
    h, m = STORE["open_time"][day]
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
    ap.add_argument("--store", choices=list(STORES), default="its",
                    help="お店（its=バルダイニングイッツ／hanaya=チーナテリアハナヤ）")
    args = ap.parse_args()
    global STORE
    STORE = STORES[args.store]

    mode = os.environ.get("POST_MODE", "dry").strip() or "dry"
    if mode not in MODE_NAMES:
        raise StoryError(f"POST_MODE は dry / check / live のどれかにしてください（今は {mode}）")
    now = datetime.now(JST)
    today = now.date().isoformat()
    day = args.day or WEEKDAYS[now.weekday()]
    print(f"🏠 {STORE['name']}（@{STORE['username']}）")
    print(f"📅 {today}（{day}）／ モード：{MODE_NAMES[mode]}")
    if day not in STORE["open_time"]:
        print(f"今日（{day}）は定休日なので、何もしません")
        return

    state = load_state()
    # 手で動かした記録（テスト・確認）は数えない。毎日の自動で動いた日だけ「もう動いた」とみなす
    if not args.manual and any(p["date"] == today and not p.get("manual") for p in state["posts"]):
        print("今日はもう動いているので、何もしません（予備の起動）")
        return

    try:
        image = pick_image(day, state)
    except StoryError as e:
        if mode == "dry":  # テスト中は、動画がまだ無くても失敗メールを送らない
            print(f"⚠️ {e}（テスト中なので、失敗扱いにはしません）")
            return
        raise
    rel = nfc(f"{day}/{image.name}")
    print(f"{'🎬 今日の動画' if is_video(image) else '🖼 今日の画像'}：{STORE['folder']}/{rel}")

    if not args.manual:
        wait_until_open(datetime.now(JST), day)

    media_id = None
    if mode == "dry":
        print("（テストなので、Instagramにはつないでいません）")
    else:
        token = (os.environ.get(STORE["token_env"], "").strip()
                 or os.environ.get("IG_ACCESS_TOKEN", "").strip())
        if not token:
            raise StoryError(f"合い鍵（{STORE['token_env']}）が設定されていません")
        url = public_url(day, image)
        user_id = check_account(token)
        cid = create_container(token, user_id, url, is_video(image))
        if mode == "check":
            print("（確認なので、ストーリーには流していません）")
        else:
            media_id = api("POST", f"{user_id}/media_publish", token, creation_id=cid)["id"]
            print(f"🎉 ストーリーに流しました！（投稿ID {media_id}）")

    state["posts"].append({"date": today, "day": day, "image": rel,
                           "mode": MODE_NAMES[mode], "media_id": media_id,
                           "time": datetime.now(JST).strftime("%H:%M"),
                           "manual": args.manual})
    if mode != "check":  # テストでも順番が回る様子を見られるように記録する
        state["last_posted"][rel] = today
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except StoryError as e:
        print(f"❌ {e}")
        sys.exit(1)
