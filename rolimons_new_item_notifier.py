"""
Rolimons「All Roblox Items」新着アイテム通知スクリプト
対象ページ: https://www.rolimons.com/roblox-catalog/all-roblox-items

デフォルトソート「Newest Created(新着順)」のページ先頭を毎回チェックし、
前回実行時になかったアイテムを新着として検知、Discordへ通知します。

------------------------------------------------------------------
事前準備
------------------------------------------------------------------
    pip install playwright requests
    playwright install chromium

Discord Webhook URLの作り方:
    Discordサーバー設定 → 連携サービス → ウェブフック → 新しいウェブフック
    → URLをコピーして環境変数 DISCORD_WEBHOOK_URL に設定する

------------------------------------------------------------------
実行方法
------------------------------------------------------------------
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxxx/yyyy"
    python rolimons_new_item_notifier.py

    # 抽出がうまくいっているか確認したいとき（通知は送らず内容を表示するだけ）
    python rolimons_new_item_notifier.py --debug

------------------------------------------------------------------
定期実行の例（15分ごと、cron / Linux・Mac）
------------------------------------------------------------------
    */15 * * * * cd /path/to/script && /usr/bin/python3 rolimons_new_item_notifier.py >> notifier.log 2>&1

Windowsの場合はタスクスケジューラで同様に15分間隔などで登録してください。

------------------------------------------------------------------
注意事項
------------------------------------------------------------------
- Rolimons側のページ構造(HTML)が変わると抽出が失敗する可能性があります。
  その場合は --debug の出力を見ながら parse_items() 内の正規表現を調整してください。
- アクセス頻度は控えめに(15分に1回程度を推奨)。連続アクセスはサイトに
  負荷をかけたり、アクセス制限を受ける原因になります。
- このスクリプトは開発環境のネットワーク制限により実サイトに対して
  動作確認ができていません。初回は必ず --debug で抽出結果を確認してから
  本番運用してください。
"""

import os
import re
import json
import sys
import argparse
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.rolimons.com/roblox-catalog/all-roblox-items"
STATE_FILE = Path(__file__).parent / "rolimons_seen_items.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 毎回チェックする件数(サイト先頭・新着順からこの件数だけ見る)
CHECK_TOP_N = 40


def fetch_rendered_text() -> str:
    """PlaywrightでJSレンダリング後のページ本文テキストを取得する"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=30000)
        # アイテムカードの描画を待つ
        page.wait_for_timeout(2000)
        text = page.inner_text("body")
        browser.close()
        return text


# 各アイテムカードは "<名前>By<作者>Price<価格>Favorites<件数>Created<日付>Updated<日付>"
# の並びで表示される。Favorites/Created/Updatedまで含めて1件分として消費することで、
# 次のアイテム名を誤って巻き込まないようにする。
# ("UGC Items"「Roblox Items」の下部プレビュー欄はFavorites/Created/Updatedを持たないため、
#  このパターンには自然とマッチせず、結果的にメイン一覧だけが対象になる)
ITEM_PATTERN = re.compile(
    r"(?P<name>.+?)By(?P<creator>.+?)Price(?P<price>Off Sale|Free|[\d,]+)"
    r"Favorites(?P<favorites>[\d,]+)"
    r"Created(?P<created>[A-Za-z]+ \d{1,2}, \d{4})"
    r"Updated(?P<updated>[A-Za-z]+ \d{1,2}, \d{4})",
    re.DOTALL,
)

# Trueの場合、作者(By)が "Roblox" のアイテムだけを対象にする
# (一般ユーザーが投稿できるUGCアイテムを除外し、Roblox公式アイテムのみ通知)
ONLY_ROBLOX_CREATED = True


def parse_items(raw_text: str):
    """ページ本文テキストから「アイテム名 / 作者 / 価格」のリストを抽出する"""
    marker = "Newest Created"
    idx = raw_text.find(marker)
    body = raw_text[idx:] if idx != -1 else raw_text

    items = []
    for m in ITEM_PATTERN.finditer(body):
        name = m.group("name").strip()
        creator = m.group("creator").strip()
        price = m.group("price").strip()
        if len(name) == 0 or len(name) > 100:
            continue
        if ONLY_ROBLOX_CREATED and creator != "Roblox":
            continue
        items.append({"name": name, "creator": creator, "price": price})
    return items


def item_key(item: dict) -> str:
    return f"{item['name']}::{item['creator']}"


def load_seen_keys() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen_keys(keys: set):
    STATE_FILE.write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def notify_discord_batch(items: list):
    """新着アイテムをまとめて1回(必要なら数回)の通知で送信する"""
    if not DISCORD_WEBHOOK_URL:
        print("警告: DISCORD_WEBHOOK_URL が未設定のため通知をスキップしました。")
        return
    if not items:
        return

    header = f"🆕 **新着アイテムを{len(items)}件検知しました**\n{URL}\n"
    lines = [
        f"・**{item['name']}** (作者: {item['creator']} / 価格: {item['price']})"
        for item in items
    ]

    # Discordの1メッセージあたり2000文字制限に収まるようチャンク分割
    chunks = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)

    for chunk in chunks:
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Discord通知エラー: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug", action="store_true", help="取得したテキストと抽出結果を表示して終了"
    )
    args = parser.parse_args()

    raw_text = fetch_rendered_text()

    if args.debug:
        print(raw_text[:5000])
        print("\n---\n抽出結果:")
        for item in parse_items(raw_text)[:CHECK_TOP_N]:
            print(item)
        return

    items = parse_items(raw_text)[:CHECK_TOP_N]
    if not items:
        print("アイテムを抽出できませんでした。--debug オプションでページ構造を確認してください。")
        sys.exit(1)

    seen_keys = load_seen_keys()

    # 初回実行時は通知せず、現状を「既知」として保存するだけにする
    if not seen_keys:
        save_seen_keys({item_key(i) for i in items})
        print(f"初回実行: {len(items)}件を既知アイテムとして登録しました。")
        return

    new_items = [i for i in items if item_key(i) not in seen_keys]
    new_items_ordered = list(reversed(new_items))  # 古い順に並べる

    notify_discord_batch(new_items_ordered)
    for item in new_items_ordered:
        print(f"検知: {item['name']}")

    save_seen_keys({item_key(i) for i in items})

    if not new_items:
        print("新着アイテムはありませんでした。")


if __name__ == "__main__":
    main()
