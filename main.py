"""life-os-bot — 個人用の人生管理 Discord BOT

チャンネル名で機能を振り分ける。スプレッドシート連携は sheets.py（現在スタブ）に委譲。

必要な環境変数:
    DISCORD_TOKEN      Discord BOT トークン
    ANTHROPIC_API_KEY  Claude API キー
    GOOGLE_SHEET_ID    スプレッドシート ID（シート連携実装後に使用）
"""

import base64
import calendar
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup
from discord.ext import tasks
from dotenv import load_dotenv

import sheets

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")  # シート連携実装後に使用

# Claude モデル（用途に応じて使い分け可能）
CLAUDE_MODEL = "claude-sonnet-4-6"

JST = ZoneInfo("Asia/Tokyo")

# 多重起動防止用 PID ファイル（Windows でも動くよう OS の一時ディレクトリを使う）
PID_FILE = os.path.join(tempfile.gettempdir(), "life-os-bot.pid")

# チャンネル名 → 機能のマッピング（.env の CHANNEL_* で上書き可能。未設定なら既定値）
CH_WEIGHT = os.environ.get("CHANNEL_WEIGHT", "体重")
CH_TODO = os.environ.get("CHANNEL_TODO", "今日やること")
CH_REVIEW = os.environ.get("CHANNEL_REVIEW", "振り返り")
CH_MONEY = os.environ.get("CHANNEL_MONEY", "お金")
CH_IDEA = os.environ.get("CHANNEL_IDEA", "アイデア")
CH_ARTICLE = os.environ.get("CHANNEL_ARTICLE", "気になる記事")
CH_ASSISTANT = os.environ.get("CHANNEL_ASSISTANT", "AI秘書")
CH_REPORT = os.environ.get("CHANNEL_REPORT", "レポート")

# BOT が自動で話しかけるチャンネル（ユーザー入力を処理する／しないは別途分岐）
AUTO_POST_CHANNELS = {CH_WEIGHT, CH_TODO, CH_REVIEW}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),  # bot.log に出力
        logging.StreamHandler(),                            # コンソールにも出力
    ],
)
logger = logging.getLogger("life-os-bot")

# ---------------------------------------------------------------------------
# クライアント初期化
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # メッセージ本文を読むために必須
intents.reactions = True  # ✅ リアクション検知のために有効化
client = discord.Client(intents=intents)

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def find_channels_by_name(name: str) -> list[discord.TextChannel]:
    """全ギルドから指定名のテキストチャンネルを探す。

    Discord はチャンネル名のラテン文字を小文字化する（例: "AI秘書" → "ai秘書"）ため、
    大文字小文字を無視して比較する。
    """
    target = name.lower()
    found = []
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() == target:
                found.append(channel)
    return found


async def download_attachment(attachment: discord.Attachment) -> tuple[bytes, str]:
    """添付ファイルをダウンロードし、(バイト列, media_type) を返す。"""
    data = await attachment.read()
    media_type = attachment.content_type or "image/png"
    # content_type に charset などが付く場合があるので前半だけ使う
    media_type = media_type.split(";")[0].strip()
    return data, media_type


async def claude_text(prompt: str, system: str | None = None, max_tokens: int = 1024) -> str:
    """Claude にテキストプロンプトを投げて返答テキストを得る。"""
    if claude is None:
        return "（ANTHROPIC_API_KEY が未設定のため Claude を呼び出せません）"
    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    resp = await claude.messages.create(**kwargs)
    return "".join(block.text for block in resp.content if block.type == "text").strip()


async def claude_vision(image_bytes: bytes, media_type: str, prompt: str,
                        max_tokens: int = 1024) -> str:
    """画像 + プロンプトを Claude に投げて返答テキストを得る。"""
    if claude is None:
        return "（ANTHROPIC_API_KEY が未設定のため Claude を呼び出せません）"
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    resp = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return "".join(block.text for block in resp.content if block.type == "text").strip()


def _extract_json(s: str) -> str:
    """文字列中から最初の { ... } までを取り出す（コードフェンス対策）。"""
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start:end + 1]
    return s


async def format_idea(text: str) -> dict:
    """アイデアのテキストを Claude で整形し、title/category/content の dict を返す。

    パース失敗時は {"title": text[:20], "category": "その他", "content": text} を返す。
    """
    fallback = {"title": text[:20], "category": "その他", "content": text}
    if claude is None:
        return fallback

    prompt = (
        "次のアイデアのメモを整理し、JSON だけを出力してください（前後に説明文は不要）。\n"
        "形式:\n"
        "{\n"
        '  "title": "アイデアを一言で表すタイトル（20字以内）",\n'
        '  "category": "ビジネス / 生活改善 / 趣味・創作 / 学び / その他 のいずれか",\n'
        '  "content": "アイデアの内容を整理・補足した文章（箇条書き可、元の意図を保ちつつ読みやすく）"\n'
        "}\n\n"
        f"メモ:\n{text}"
    )
    try:
        raw = await claude_text(prompt)
        data = json.loads(_extract_json(raw))
        return {
            "title": (str(data.get("title")).strip() or text[:20]) if data.get("title") else text[:20],
            "category": (str(data.get("category")).strip() or "その他") if data.get("category") else "その他",
            "content": (str(data.get("content")).strip() or text) if data.get("content") else text,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("format_idea のJSONパースに失敗、フォールバックします: %s", e)
        return fallback


# ---------------------------------------------------------------------------
# スケジュール送信（JST 7:00 / 22:00）
# ---------------------------------------------------------------------------
@tasks.loop(time=time(hour=7, minute=0, tzinfo=JST))
async def morning_post():
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    key = f"morning-{today_str}"
    if key in _fired_today:
        return
    _fired_today.add(key)
    logger.info("朝の定期投稿を実行")

    # 体重チャンネル
    for ch in find_channels_by_name(CH_WEIGHT):
        await ch.send("おはようございます！今日の体重を教えてください")

    # 今日やることチャンネル（AI生成のパーソナライズ挨拶）
    tasks_list = sheets.get_incomplete_tasks()
    today = date.today()
    yesterday = today - timedelta(days=1)

    # 1) 直近1週間（8日分）の体重変化＝最初と最後の差
    weight_change = None
    weight_rows = sheets._rows_by_period(sheets.WS_WEIGHT, today - timedelta(days=7), today)
    if len(weight_rows) >= 2:
        weight_rows.sort(key=lambda r: sheets._parse_date(r[0]) or date.min)
        try:
            weight_change = round(float(weight_rows[-1][1]) - float(weight_rows[0][1]), 1)
        except (IndexError, ValueError):
            weight_change = None

    # 2) 昨日の振り返り
    diary_entries = sheets.get_diary_by_period(yesterday, yesterday)
    yesterday_diary = "\n".join(e["content"] for e in diary_entries if e.get("content")) or None

    # 3) 昨日保存した記事の数
    yesterday_articles = len(sheets._rows_by_period(sheets.WS_ARTICLE, yesterday, yesterday))

    # 4) コンテキストにまとめて挨拶を生成
    context = {
        "weight_change": weight_change,
        "yesterday_diary": yesterday_diary,
        "yesterday_articles": yesterday_articles,
    }
    greeting = await generate_morning_greeting(tasks_list, context)

    # 5) 今日やることチャンネルに送信
    for ch in find_channels_by_name(CH_TODO):
        await ch.send(greeting)


@tasks.loop(time=time(hour=22, minute=0, tzinfo=JST))
async def night_post():
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    key = f"night-{today_str}"
    if key in _fired_today:
        return
    _fired_today.add(key)
    logger.info("夜の定期投稿を実行")
    for ch in find_channels_by_name(CH_REVIEW):
        await ch.send("今日はどんな1日でしたか？記録を取るので雑談ベースで教えてください")


@tasks.loop(time=time(hour=21, minute=0, tzinfo=JST))
async def monthly_report_post():
    # 毎日21時に起動し、その日が月末日のときだけレポートを送る
    now = datetime.now(JST)
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day:
        return
    key = f"monthly-{now.year}-{now.month}"
    if key in _fired_today:
        return
    _fired_today.add(key)
    logger.info("月末レポートを実行: %d年%d月", now.year, now.month)
    await send_monthly_report(now.year, now.month)


@morning_post.before_loop
@night_post.before_loop
@monthly_report_post.before_loop
async def _before_scheduled():
    await client.wait_until_ready()


# ---------------------------------------------------------------------------
# 各チャンネルのハンドラ
# ---------------------------------------------------------------------------
async def handle_weight(message: discord.Message):
    # 画像が添付されていれば Claude のビジョンで数値を読み取る
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if images:
        data, media_type = await download_attachment(images[0])
        result = await claude_vision(
            data, media_type,
            "この画像（体重計の表示やスクリーンショット）から体重の数値だけを読み取り、"
            "kg 単位の数字のみを返してください。例: 65.2  数値が読み取れない場合は "
            "'不明' とだけ返してください。",
        )
        value = _parse_weight(result)
        if value is None:
            await message.reply(f"画像から体重を読み取れませんでした（読み取り結果: {result}）")
            return
        sheets.add_weight(value)
        await message.reply(f"体重 {value}kg を記録しました")
        return

    # テキストの数値
    value = _parse_weight(message.content)
    if value is not None:
        sheets.add_weight(value)
        await message.reply(f"体重 {value}kg を記録しました")
    else:
        await message.reply("体重の数値（例: 65.2）または画像を送ってください")


def _parse_weight(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    if not m:
        return None
    try:
        v = float(m.group())
    except ValueError:
        return None
    # 妥当な体重レンジでフィルタ（誤読防止）
    if 20.0 <= v <= 300.0:
        return v
    return None


async def handle_review(message: discord.Message):
    text = message.content.strip()
    if not text:
        return
    # 日記として記録
    sheets.add_diary(text)

    # 明日のタスクに相当する内容を抽出 → バックログ（実行予定日なし）で追加
    extracted = await claude_text(
        f"以下は今日の振り返りの文章です。この中から『明日やるべきタスク』に相当する"
        f"具体的な行動だけを抽出してください。タスクが複数あれば1行に1つずつ、"
        f"タスクが無ければ空行のみを返してください。前置きや説明は不要です。\n\n"
        f"---\n{text}\n---",
    )
    for line in extracted.splitlines():
        t = line.strip().lstrip("・-*0123456789. ").strip()
        if t:
            sheets.add_task(t)  # scheduled_date なし＝バックログ

    # バックログを取得し、明日やるものを番号で選んでもらう
    backlog = sheets.get_backlog_tasks()
    if backlog:
        body = "\n".join(f"{i + 1}. {b['content']}" for i, b in enumerate(backlog))
        reply_text = (
            f"📋 バックログに {len(backlog)} 件あります：\n"
            f"{body}\n\n"
            "明日やるものを番号で返信してください（例: 1,2）\n"
            "なければ「なし」と返信してください。"
        )
    else:
        reply_text = "記録しました"

    # note記事・Xポスト生成の案内を末尾に付ける
    reply_text += (
        "\n\n──────────────\n"
        f"{NOTE_EMOJI} note記事を作る　{X_EMOJI} Xポストを作る"
    )
    sent = await message.reply(reply_text)

    if backlog:
        pending_task_selections[message.channel.id] = backlog

    # 📝 / 🐦 リアクションで note記事 / Xポストを生成できるよう登録
    pending_content_generations[sent.id] = text
    for emoji in (NOTE_EMOJI, X_EMOJI):
        try:
            await sent.add_reaction(emoji)
        except discord.HTTPException as e:
            logger.warning("リアクション付与に失敗: %s", e)


async def handle_task_selection(message: discord.Message):
    """振り返り後、番号で選ばれたバックログを翌日のタスクに設定する。"""
    backlog = pending_task_selections.pop(message.channel.id, None)
    if backlog is None:
        return

    text = message.content.strip()
    if text in ("なし", "ナシ", "無し", "0"):
        await message.reply("了解です！")
        return

    # カンマ・スペース・読点で分割
    parts = re.split(r"[,\s、，]+", text)
    rows: list[int] = []
    names: list[str] = []
    seen: set[int] = set()
    for p in parts:
        p = p.strip()
        if not p.isdigit():
            continue
        idx = int(p) - 1
        if 0 <= idx < len(backlog) and idx not in seen:
            seen.add(idx)
            rows.append(backlog[idx]["row"])
            names.append(backlog[idx]["content"])

    if not rows:
        # 有効な番号が無ければ選択待ちを維持して再入力を促す
        pending_task_selections[message.channel.id] = backlog
        await message.reply("番号が読み取れませんでした。例: 1,2 のように返信してください（なければ「なし」）。")
        return

    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    sheets.schedule_tasks(rows, tomorrow)
    body = "\n".join(f"・{n}" for n in names)
    await message.reply(f"✅ 明日のタスクに設定しました！\n{body}")


async def handle_money(message: discord.Message):
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]

    instruction = (
        "支出の情報から『金額（整数の円）』『ジャンル』『備考』を抽出してください。"
        "ジャンルは 食費 / 日用品 / 交通費 / 交際費 / 娯楽 / 医療 / 衣服 / 住居 / 通信 / その他 "
        "から最も近いものを1つ選んでください。備考は店名や品目など簡潔に。"
        "必ず次の形式の1行だけで返してください: 金額|ジャンル|備考  "
        "例: 1200|食費|ランチ  読み取れない場合は: 0|その他|不明"
    )

    if images:
        data, media_type = await download_attachment(images[0])
        result = await claude_vision(
            data, media_type,
            "この画像はレシートまたは支払いのスクリーンショットです。" + instruction,
        )
    else:
        text = message.content.strip()
        if not text:
            await message.reply("支出の内容（テキストまたは画像）を送ってください")
            return
        result = await claude_text(f"次の支出メモから情報を抽出してください。{instruction}\n\n{text}")

    amount, genre, note = _parse_expense(result)
    if amount is None:
        await message.reply(f"金額を読み取れませんでした（解析結果: {result}）")
        return
    sheets.add_expense(amount, genre, note)
    detail = " / ".join(x for x in (genre, note) if x)
    await message.reply(f"記録しました（{amount:,}円 / {detail}）")


def _parse_expense(result: str) -> tuple[int | None, str, str]:
    line = (result or "").strip().splitlines()[0] if result.strip() else ""
    if "|" not in line:
        return None, "", ""
    fields = [f.strip() for f in line.split("|")]
    amount_str = fields[0] if len(fields) > 0 else ""
    genre = fields[1] if len(fields) > 1 else ""
    note = fields[2] if len(fields) > 2 else ""
    digits = re.sub(r"[^\d]", "", amount_str)
    if not digits or int(digits) == 0:
        return None, genre, note
    return int(digits), genre or "その他", note or "不明"


async def handle_idea(message: discord.Message):
    text = message.content.strip()
    if not text:
        await message.reply("アイデアの内容を送ってください")
        return

    result = await format_idea(text)
    title = result["title"]
    category = result["category"]
    content = result["content"]

    sheets.add_idea(title, category, content)

    reply = (
        "💡 記録しました！\n\n"
        f"**{title}**\n"
        f"カテゴリ：{category}\n\n"
        f"{content}\n\n"
        "──────────────\n"
        "📋 タスクに追加しますか？ → ✅を押してください"
    )
    sent = await message.reply(reply)
    # ✅ が押されたときにタスク化できるよう、返信メッセージ(reply.id)とタイトルを紐付ける
    pending_idea_tasks[sent.id] = title
    try:
        await sent.add_reaction(TASK_EMOJI)
    except discord.HTTPException as e:
        logger.warning("リアクション付与に失敗: %s", e)


async def handle_article(message: discord.Message):
    url = _extract_url(message.content)
    if not url:
        await message.reply("記事の URL を送ってください")
        return

    title, page_text = await fetch_page(url)
    if page_text is None:
        await message.reply(f"ページを取得できませんでした: {url}")
        return

    summary = await claude_text(
        f"次の記事を日本語で3〜5行に要約してください。要点を箇条書きで。\n\n"
        f"タイトル: {title}\n\n本文:\n{page_text[:6000]}",
    )
    sheets.add_article(url, summary)
    await message.reply(f"【{title}】\n{summary}")


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text or "")
    return m.group() if m else None


async def fetch_page(url: str) -> tuple[str, str | None]:
    """URL からタイトルと本文テキストを取得する。"""
    headers = {"User-Agent": "Mozilla/5.0 (life-os-bot)"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                html = await resp.text()
    except Exception as e:  # noqa: BLE001
        logger.warning("ページ取得失敗 %s: %s", url, e)
        return url, None

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    # スクリプト・スタイルを除去して本文テキストを抽出
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return title, text


SECRETARY_SYSTEM = (
    "あなたは個人用の人生管理AIアシスタントです。\n"
    "ユーザーのスプレッドシートデータを参照・編集できます。\n"
    "質問に答えるだけでなく、データの追加・修正もツールを使って実行してください。"
)

# Claude に見せるツール定義（名前はツール名、内部で sheets の各関数にディスパッチ）
SECRETARY_TOOLS = [
    {
        "name": "record_expense",
        "description": "支出を家計簿に新規追加する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "金額（円）"},
                "category": {"type": "string", "description": "ジャンル（食費/日用品/交通費/交際費/娯楽/医療/衣服/住居/通信/その他 など）"},
                "description": {"type": "string", "description": "備考（店名や品目）"},
                "date": {"type": "string", "description": "日付 YYYY-MM-DD（省略時は今日）"},
            },
            "required": ["amount", "category", "description"],
        },
    },
    {
        "name": "update_expense",
        "description": "既存の支出を修正する。日付と現在の備考で対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "対象支出の日付 YYYY-MM-DD"},
                "current_description": {"type": "string", "description": "対象支出の現在の備考（D列）"},
                "amount": {"type": "integer", "description": "新しい金額（円）"},
                "category": {"type": "string", "description": "新しいジャンル"},
                "description": {"type": "string", "description": "新しい備考"},
            },
            "required": ["date", "current_description", "amount", "category", "description"],
        },
    },
    {
        "name": "delete_expense",
        "description": "支出を削除する。日付と備考で対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "対象支出の日付 YYYY-MM-DD"},
                "description": {"type": "string", "description": "対象支出の備考（D列）"},
            },
            "required": ["date", "description"],
        },
    },
    {
        "name": "add_task",
        "description": "タスクをバックログ（実行予定日なし）に追加する。",
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "タスクの内容"}},
            "required": ["task"],
        },
    },
    {
        "name": "complete_task",
        "description": "タスクを完了にする。内容で対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "対象タスクの内容"}},
            "required": ["content"],
        },
    },
    {
        "name": "update_task",
        "description": "タスクの内容を修正する。現在の内容で対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "old_content": {"type": "string", "description": "現在のタスク内容"},
                "new_content": {"type": "string", "description": "新しいタスク内容"},
            },
            "required": ["old_content", "new_content"],
        },
    },
    {
        "name": "delete_task",
        "description": "タスクを削除する。内容で対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "対象タスクの内容"}},
            "required": ["content"],
        },
    },
    {
        "name": "record_idea",
        "description": "アイデアを新規追加する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "アイデアのタイトル"},
                "category": {"type": "string", "description": "カテゴリ（ビジネス/生活改善/趣味・創作/学び/その他）"},
                "content": {"type": "string", "description": "アイデアの内容"},
            },
            "required": ["title", "category", "content"],
        },
    },
    {
        "name": "update_idea",
        "description": "既存のアイデアを修正する。タイトルで対象を特定する。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "対象アイデアのタイトル（特定用。更新後も同じタイトル）"},
                "category": {"type": "string", "description": "新しいカテゴリ"},
                "content": {"type": "string", "description": "新しい内容"},
            },
            "required": ["title", "category", "content"],
        },
    },
    {
        "name": "get_period_analysis",
        "description": "指定期間の振り返り日記を取得して、傾向や気づきを分析・レポートする材料にする。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "開始日 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "終了日 YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
]


def _period_diary_text(start_date: str, end_date: str) -> str:
    """指定期間の振り返り日記をテキストで返す（get_period_analysis ツールの実体）。"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return "日付は YYYY-MM-DD 形式で指定してください。"
    picked = []
    for r in sheets.get_recent_diary(limit=0):  # limit=0 で全件
        d = sheets._parse_date(r.get("日付"))
        if d and start <= d <= end:
            picked.append(f"{r.get('日付')}: {r.get('本文')}")
    if not picked:
        return f"{start_date}〜{end_date} の振り返り日記はありませんでした。"
    return f"{start_date}〜{end_date} の振り返り日記:\n" + "\n".join(picked)


def _run_secretary_tool(name: str, inp: dict) -> str:
    """ツール名と入力を受け取り、sheets を操作して結果テキストを返す。"""
    try:
        if name == "record_expense":
            when = None
            if inp.get("date"):
                try:
                    when = datetime.strptime(inp["date"], "%Y-%m-%d")
                except ValueError:
                    when = None
            sheets.add_expense(int(inp["amount"]), inp.get("category", ""),
                               inp.get("description", ""), when=when)
            return "支出を記録しました。"

        if name == "update_expense":
            row = sheets.find_expense_row(inp["date"], inp["current_description"])
            if row is None:
                return "該当する支出が見つかりませんでした。"
            sheets.update_expense_row(row, int(inp["amount"]), inp["description"], inp["category"])
            return "支出を更新しました。"

        if name == "delete_expense":
            row = sheets.find_expense_row(inp["date"], inp["description"])
            if row is None:
                return "該当する支出が見つかりませんでした。"
            sheets.delete_expense_row(row)
            return "支出を削除しました。"

        if name == "add_task":
            sheets.add_task(inp["task"])  # バックログ
            return "タスクをバックログに追加しました。"

        if name == "complete_task":
            ok = sheets.complete_task_by_content(inp["content"])
            return "タスクを完了にしました。" if ok else "該当するタスクが見つかりませんでした。"

        if name == "update_task":
            ok = sheets.update_task_by_content(inp["old_content"], inp["new_content"])
            return "タスクを更新しました。" if ok else "該当するタスクが見つかりませんでした。"

        if name == "delete_task":
            ok = sheets.delete_task_by_content(inp["content"])
            return "タスクを削除しました。" if ok else "該当するタスクが見つかりませんでした。"

        if name == "record_idea":
            sheets.add_idea(inp["title"], inp.get("category", ""), inp.get("content", ""))
            return "アイデアを記録しました。"

        if name == "update_idea":
            row = sheets.find_idea_row(inp["title"])
            if row is None:
                return "該当するアイデアが見つかりませんでした。"
            sheets.update_idea_row(row, inp["title"], inp.get("category", ""), inp.get("content", ""))
            return "アイデアを更新しました。"

        if name == "get_period_analysis":
            return _period_diary_text(inp["start_date"], inp["end_date"])

        return f"未知のツールです: {name}"
    except Exception as e:  # noqa: BLE001
        logger.exception("ツール実行エラー (%s): %s", name, e)
        return f"ツール実行中にエラーが発生しました: {e}"


async def answer_secretary_question(question: str, image_bytes: bytes | None = None,
                                    media_type: str = "image/jpeg") -> str:
    """tool use 対応の AI秘書。スプレッドシートを参照・編集しながら回答する。"""
    if claude is None:
        return "（ANTHROPIC_API_KEY が未設定のため回答できません）"

    # コンテキストデータ（各シートのダンプ＋アイデア一覧）
    context = sheets.dump_for_assistant()
    ideas = sheets.get_recent_ideas()
    ideas_lines = [f"- {i.get('日付')} [{i.get('カテゴリ')}] {i.get('タイトル')}: {i.get('内容')}"
                   for i in ideas]
    ideas_text = "\n".join(ideas_lines) if ideas_lines else "（アイデアはまだありません）"

    user_content: list[dict] = []
    if image_bytes is not None:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    user_content.append({
        "type": "text",
        "text": (
            f"{question}\n\n"
            f"# 参考データ（スプレッドシート）\n{context}\n\n"
            f"# アイデア一覧\n{ideas_text}"
        ),
    })

    messages: list[dict] = [{"role": "user", "content": user_content}]

    for _ in range(8):  # ツール往復の上限
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SECRETARY_SYSTEM,
            tools=SECRETARY_TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = _run_secretary_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue
        # end_turn など: 最終テキストを返す
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    return "（ツール実行の繰り返しが上限に達しました。もう一度お試しください）"


async def handle_secretary(message: discord.Message):
    question = message.content.strip()

    image_bytes = None
    media_type = "image/jpeg"
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if images:
        image_bytes, media_type = await download_attachment(images[0])

    if not question and image_bytes is None:
        return

    try:
        async with message.channel.typing():
            answer = await answer_secretary_question(
                question, image_bytes=image_bytes, media_type=media_type,
            )
        # Discord の2000字制限に配慮して必要なら切り詰める
        if len(answer) > 1990:
            answer = answer[:1990] + "…"
        await message.reply(answer or "（回答が空でした）")
    except Exception as e:  # noqa: BLE001
        logger.exception("AI秘書でエラー: %s", e)
        await message.reply(f"処理中にエラーが発生しました：{e}")


# ---------------------------------------------------------------------------
# 月末レポート
# ---------------------------------------------------------------------------
async def generate_monthly_report(current: dict, prev: dict, year: int, month: int) -> str:
    """当月／前月のデータから統計テキストを組み立て、Claude のコメントを添えて返す。

    current / prev のキー: expense, ideas, articles, tasks_done, first_weight, last_weight
    """
    lines = []
    lines.append(
        f"・支出：{current['expense']:,}円"
        f"（前月比 {current['expense'] - prev['expense']:+,}円）"
    )

    fw, lw = current["first_weight"], current["last_weight"]
    if fw is not None and lw is not None:
        lines.append(f"・体重：{fw}kg → {lw}kg（{lw - fw:+.1f}kg）")
    elif lw is not None:
        lines.append(f"・体重：{lw}kg")
    else:
        lines.append("・体重：記録なし")

    lines.append(f"・完了タスク：{current['tasks_done']}件（前月 {prev['tasks_done']}件）")
    lines.append(f"・アイデア：{current['ideas']}件（前月 {prev['ideas']}件）")
    lines.append(f"・記事：{current['articles']}件（前月 {prev['articles']}件）")
    stats = "\n".join(lines)

    comment = await claude_text(
        "次は個人の月次活動サマリーです。データを踏まえて、ねぎらいと前向きな気づきを"
        "2〜3行の日本語コメントにしてください。前置きや見出しは不要です。\n\n" + stats,
    )
    return f"📊 **{year}年{month}月のレポート**\n\n{stats}\n\n{comment}"


async def generate_period_analysis(entries: list[dict], start_str: str, end_str: str) -> str:
    """期間の日記エントリを分析し、悩み・キーワード・AI分析の3セクションで返す。"""
    if not entries:
        return f"{start_str}〜{end_str} の振り返り日記はありませんでした。"
    body = "\n".join(f"{e.get('date')}: {e.get('content')}" for e in entries)
    return await claude_text(
        f"以下は {start_str}〜{end_str} の振り返り日記です。これを分析し、"
        "次の形式で日本語で出力してください（各セクションの見出しは必ず付ける）：\n"
        "【最近の悩み・課題】\n（箇条書き）\n"
        "【よく出てくるキーワード・テーマ】\n（箇条書き）\n"
        "【AI分析】\n（2〜3行）\n\n"
        f"---\n{body}\n---",
        max_tokens=2048,
    )


async def generate_morning_greeting(tasks: list[str], context: dict) -> str:
    """朝の通知をパーソナライズして生成する。

    context: weight_change(float|None) / yesterday_diary(str|None) / yesterday_articles(int)
    """
    # API が無い場合のフォールバック（朝の通知が止まらないように）
    if claude is None:
        if tasks:
            body = "\n".join(f"・{t}" for t in tasks)
            return f"おはようございます。\n本日の優先タスクはこちらです。\n{body}"
        return "おはようございます。\n今日のタスクはありません。"

    facts = []
    wc = context.get("weight_change")
    if wc is not None:
        facts.append(f"直近1週間の体重変化は {wc:+.1f}kg")
    if context.get("yesterday_diary"):
        facts.append(f"昨日の振り返り: {context['yesterday_diary']}")
    if context.get("yesterday_articles"):
        facts.append(f"昨日保存した記事は {context['yesterday_articles']} 件")
    facts_text = "\n".join(f"- {f}" for f in facts) if facts else "（特になし）"

    task_text = "\n".join(f"- {t}" for t in tasks) if tasks else "（タスクなし）"

    return await claude_text(
        "次の情報をもとに、朝の通知メッセージを作ってください。\n"
        "条件:\n"
        "・「おはようございます。」で始める\n"
        "・体重変化や昨日の実績があれば1〜2文で自然に触れる（無い情報には触れない）\n"
        "・「本日の優先タスクはこちらです。」に続けてタスクを箇条書き\n"
        "・タスクが無ければ「今日のタスクはありません」と書く\n"
        "・全体200字以内、フレンドリーに\n\n"
        f"# 昨日までの情報\n{facts_text}\n\n"
        f"# 今日のタスク\n{task_text}",
        max_tokens=512,
    )


async def generate_note_article(diary_content: str) -> str:
    """振り返り日記をもとに note に投稿できる記事を生成する。"""
    if claude is None:
        return "（ANTHROPIC_API_KEY が未設定のため生成できません）"
    return await claude_text(
        "以下は個人の振り返り日記です。これをもとに note に投稿できる記事を書いてください。\n"
        "条件:\n"
        "・冒頭に「# タイトル」形式でタイトルを付ける\n"
        "・800〜1200字程度\n"
        "・日記口調ではなく、読者に向けた記事として書く\n"
        "・体験や気づきを、読者が共感・活用できる形に昇華する\n\n"
        f"---\n{diary_content}\n---",
        max_tokens=2048,
    )


async def generate_x_posts(diary_content: str) -> str:
    """振り返り日記をもとに X のポスト案を3つ生成する。"""
    if claude is None:
        return "（ANTHROPIC_API_KEY が未設定のため生成できません）"
    return await claude_text(
        "以下は個人の振り返り日記です。これをもとに X（旧Twitter）のポスト案を3つ作ってください。\n"
        "条件:\n"
        "・各投稿は140字以内\n"
        "・気づき・学び・面白い視点を短くまとめる\n"
        "・ハッシュタグは付けない\n"
        "・1. 2. 3. と番号付きで3案\n\n"
        f"---\n{diary_content}\n---",
        max_tokens=1024,
    )


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Discord の文字数制限を超えないよう、text を limit 字ずつに分割する。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _collect_month_stats(year: int, month: int) -> dict:
    """月末レポート用に、指定月の各種集計を dict で返す。"""
    first_w, last_w = sheets.get_monthly_weight_change(year, month)
    return {
        "expense": sheets.get_monthly_expense_total(year, month),
        "ideas": sheets.get_monthly_count(sheets.WS_IDEA, year, month),
        "articles": sheets.get_monthly_count(sheets.WS_ARTICLE, year, month),
        "tasks_done": sheets.get_completed_tasks_monthly(year, month),
        "first_weight": first_w,
        "last_weight": last_w,
    }


async def send_monthly_report(year: int, month: int) -> None:
    """当月と前月を集計し、レポートを生成して CHANNEL_REPORT に送信する。"""
    current = _collect_month_stats(year, month)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev = _collect_month_stats(prev_year, prev_month)

    report = await generate_monthly_report(current, prev, year, month)
    for ch in find_channels_by_name(CH_REPORT):
        await ch.send(report)


# チャンネル名（小文字に正規化）→ ハンドラ
# Discord がラテン文字を小文字化するため、キーも小文字で持つ。
HANDLERS = {
    name.lower(): handler
    for name, handler in {
        CH_WEIGHT: handle_weight,
        CH_REVIEW: handle_review,
        CH_MONEY: handle_money,
        CH_IDEA: handle_idea,
        CH_ARTICLE: handle_article,
        CH_ASSISTANT: handle_secretary,
        # CH_TODO はユーザー入力不要なのでハンドラなし
    }.items()
}


# ---------------------------------------------------------------------------
# イベント
# ---------------------------------------------------------------------------
# BOT返信メッセージID → タスク文字列（アイデアのタイトル）
# メモリ保持のため、BOT を再起動すると過去の返信の ✅ は無効になる（起動後のアイデアのみ有効）。
pending_idea_tasks: dict[int, str] = {}
TASK_EMOJI = "✅"

# 振り返りチャンネルID → 提示したバックログ一覧（[{"row", "content"}, ...]）
# このチャンネルからの次の発言は handle_task_selection で番号選択として処理する。
pending_task_selections: dict[int, list[dict]] = {}

# 振り返りのBOT返信メッセージID → 整形済み日記内容
# 📝/🐦 リアクションで note記事 / Xポストを生成する。
pending_content_generations: dict[int, str] = {}
NOTE_EMOJI = "📝"
X_EMOJI = "🐦"

# 当日に実行済みの定期通知キー（"morning-2026-06-11" など）。重複送信を防ぐ。
_fired_today: set[str] = set()


@client.event
async def on_ready():
    logger.info("ログインしました: %s (id=%s)", client.user, client.user.id)
    if not morning_post.is_running():
        morning_post.start()
    if not night_post.is_running():
        night_post.start()
    if not monthly_report_post.is_running():
        monthly_report_post.start()


def _looks_like_selection(text: str) -> bool:
    """振り返りの番号選択っぽい入力か判定する（数字＋区切り、または「なし」系）。"""
    t = (text or "").strip()
    if t in ("なし", "ナシ", "無し", "0"):
        return True
    return bool(re.fullmatch(r"[\d,\s、，]+", t))


@client.event
async def on_message(message: discord.Message):
    # BOT 自身（や他の BOT）のメッセージは無視
    if message.author.bot:
        return
    # DM は対象外
    if message.guild is None:
        return

    channel_name = getattr(message.channel, "name", None)
    # Discord はラテン文字を小文字化するため、小文字に正規化して照合する。
    handler = HANDLERS.get((channel_name or "").lower())
    if handler is None:
        return

    try:
        # 振り返りチャンネル: 番号入力待ちでも、入力が「選択っぽい」ときだけ選択処理に回す。
        # 普通の文章なら待ち状態を解除して通常の振り返りとして扱う（番号と誤認しない）。
        if handler is handle_review and message.channel.id in pending_task_selections:
            if _looks_like_selection(message.content):
                await handle_task_selection(message)
            else:
                pending_task_selections.pop(message.channel.id, None)
                await handle_review(message)
        else:
            await handler(message)
    except Exception as e:  # noqa: BLE001
        logger.exception("ハンドラでエラー (%s): %s", channel_name, e)
        await message.reply("処理中にエラーが発生しました。ログを確認してください。")


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """リアクションを検知して処理する（✅:アイデア→タスク / 📝🐦:note・Xポスト生成）。"""
    # BOT 自身のリアクションは無視
    if client.user and payload.user_id == client.user.id:
        return
    emoji = str(payload.emoji)

    # --- アイデア返信の ✅ → タスク追加 ---
    if emoji == TASK_EMOJI and payload.message_id in pending_idea_tasks:
        # 二重追加を防ぐため、先に取り出して除去する
        title = pending_idea_tasks.pop(payload.message_id)
        try:
            # 実行予定日＝翌日をセット（翌朝の「今日のやること」に表示される）
            tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            sheets.add_task(title, scheduled_date=tomorrow)
        except Exception as e:  # noqa: BLE001
            logger.exception("リアクションからのタスク追加に失敗: %s", e)
            return
        channel = client.get_channel(payload.channel_id)
        if channel is not None:
            try:
                await channel.send(
                    f"✅ {title} をタスクに追加しました！"
                    "明日の朝から『今日のやること』に表示されます。"
                )
            except discord.HTTPException as e:
                logger.warning("タスク追加の通知送信に失敗: %s", e)
        return

    # --- 振り返り返信の 📝 / 🐦 → note記事 / Xポスト生成 ---
    if emoji in (NOTE_EMOJI, X_EMOJI) and payload.message_id in pending_content_generations:
        diary = pending_content_generations[payload.message_id]  # 再生成できるよう保持
        channel = client.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            async with channel.typing():
                if emoji == NOTE_EMOJI:
                    header = "📝 **note記事案**\n\n"
                    content = await generate_note_article(diary)
                else:
                    header = "🐦 **Xポスト案**\n\n"
                    content = await generate_x_posts(diary)
            for chunk in _split_message(header + content, 1900):
                await channel.send(chunk)
        except Exception as e:  # noqa: BLE001
            logger.exception("コンテンツ生成エラー: %s", e)
            try:
                await channel.send(f"生成中にエラーが発生しました：{e}")
            except discord.HTTPException:
                pass
        return


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
def _is_process_running(pid: int) -> bool:
    """指定 PID のプロセスが生きているかを判定する（Windows / POSIX 両対応）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在するが権限が無い
        return True


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("環境変数 DISCORD_TOKEN が設定されていません。")
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY が未設定です。Claude 連携機能は動作しません。")
    # log_handler=None で discord.py 独自のロガー設定を無効化し、
    # ルートロガー（bot.log + コンソール）に集約する
    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    # --- 多重起動防止（PIDファイル）---
    if os.path.exists(PID_FILE):
        old_pid = None
        try:
            with open(PID_FILE, encoding="utf-8") as f:
                old_pid = int(f.read().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _is_process_running(old_pid):
            print(f"すでに起動中です (PID={old_pid})。")
            sys.exit(1)
        # 死んでいる場合は古いファイルが残っているだけなので続行

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    try:
        main()
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
