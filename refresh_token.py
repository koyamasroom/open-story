#!/usr/bin/env python3
"""Instagram の合い鍵（長期アクセストークン・60日有効）を延長する。

週1回 GitHub の上で動く。
- 延長して鍵の文字が変わらなければ、それで終わり
- 鍵の文字が変わったら、GitHub の金庫（Secrets）の IG_ACCESS_TOKEN を新しい鍵に入れ替える
  （そのために GH_PAT＝金庫を書きかえてよい許可証 が必要。無ければ失敗にしてメールで知らせる）
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def main():
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        print("❌ 合い鍵（IG_ACCESS_TOKEN）が設定されていません")
        return 1
    q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(f"https://graph.instagram.com/refresh_access_token?{q}",
                                    timeout=60) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").replace(token, "***")
        print(f"❌ 延長に失敗しました（{e.code}）: {body}")
        print("   鍵が切れている可能性があります。説明書の「合い鍵を作り直す」を見てください。")
        return 1

    new = data["access_token"]
    print(f"::add-mask::{new}")  # ログに鍵が出ないように隠す
    days = int(data.get("expires_in", 0)) // 86400
    print(f"✅ 合い鍵を延長しました（あと約{days}日有効）")
    if new == token:
        return 0

    pat = os.environ.get("GH_PAT", "").strip()
    if not pat:
        print("❌ 鍵の文字が変わりましたが、金庫を書きかえる許可証（GH_PAT）がありません。")
        print("   説明書の「GH_PAT を入れる」を見てください。")
        return 1
    subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN",
                    "--repo", os.environ["GITHUB_REPOSITORY"]],
                   input=new, text=True, check=True,
                   env={**os.environ, "GH_TOKEN": pat})
    print("✅ 金庫の合い鍵を新しいものに入れ替えました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
