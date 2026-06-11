"""スプレッドシート連携モジュール（gspread + サービスアカウント）

GOOGLE_SHEET_ID（環境変数）と credentials.json（サービスアカウント鍵）の両方が
揃っている場合のみ実際の Google スプレッドシートに読み書きする。
どちらかが無い／接続に失敗した場合は、ログ出力だけのスタブ動作にフォールバックする。
（＝認証情報が無くても BOT 自体は動き続ける）

ワークシート（タブ）は初回アクセス時に自動作成し、1行目にヘッダーを入れる。
日付は YYYY-MM-DD（ハイフン区切り）で統一する。

シート構成:
    家計簿 : 日付 / 金額（円） / ジャンル / 備考
    体重   : 日付 / 体重kg
    日記   : 日付 / 本文
    タスク : 日付 / 内容 / 完了 / 実行予定日
    アイデア: 日付 / タイトル / カテゴリ / 内容
    記事   : 日付 / URL / 要約
"""

import calendar
import json
import logging
import os
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("life-os-bot.sheets")

CREDENTIALS_FILE = "credentials.json"

# open_by_key には spreadsheets スコープがあれば足りる
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ワークシート名
WS_EXPENSE = "家計簿"
WS_WEIGHT = "体重"
WS_DIARY = "日記"
WS_TASK = "タスク"
WS_IDEA = "アイデア"
WS_ARTICLE = "記事"

# ワークシート名 → ヘッダー行
WORKSHEETS: dict[str, list[str]] = {
    WS_EXPENSE: ["日付", "金額（円）", "ジャンル", "備考"],
    WS_WEIGHT: ["日付", "体重kg"],
    WS_DIARY: ["日付", "本文"],
    WS_TASK: ["日付", "内容", "完了", "実行予定日"],
    WS_IDEA: ["日付", "タイトル", "カテゴリ", "内容"],
    WS_ARTICLE: ["日付", "URL", "要約"],
}

# 「完了」とみなす値
_DONE_VALUES = {"true", "1", "✓", "✔", "完了", "done", "yes", "y", "○", "◯", "済"}

# 接続のキャッシュ（None = 未試行 / False = 無効 / Spreadsheet = 有効）
_spreadsheet = None
_state: Optional[bool] = None
# ヘッダー確認済みワークシート名（プロセス内で1回だけ確認）
_verified: set[str] = set()


def _date(when: Optional[datetime]) -> str:
    """YYYY-MM-DD 形式の日付文字列を返す。"""
    return (when or datetime.now()).strftime("%Y-%m-%d")


def _parse_date(value) -> Optional[date]:
    """セルの日付文字列を date に変換する。

    新形式 "%Y-%m-%d"（ハイフン）と旧形式 "%Y/%m/%d"（スラッシュ）の両方に対応。
    どちらでもパースできない場合は None を返す。
    """
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_done(value) -> bool:
    return str(value).strip().lower() in _DONE_VALUES


def _connect():
    """スプレッドシートに接続（初回のみ）。失敗・未設定なら None を返す。"""
    global _spreadsheet, _state
    if _state is not None:
        return _spreadsheet

    # .env 読み込み後に評価されるよう、ここ（接続時）で環境変数を取得する
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    # 認証情報は「環境変数の JSON 文字列」または「credentials.json ファイル」のどちらでも可
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    has_creds = bool(creds_json) or os.path.exists(CREDENTIALS_FILE)
    if not sheet_id or not has_creds:
        logger.warning(
            "スプレッドシート未設定（GOOGLE_SHEET_ID=%s, 認証情報=%s）。スタブ動作で続行します。",
            bool(sheet_id), has_creds,
        )
        _state = False
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        if creds_json:
            # 環境変数の JSON 文字列から認証（GitHub 公開／クラウドデプロイ向け）
            info = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            # ローカルの credentials.json ファイルから認証
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        gc = gspread.authorize(creds)
        _spreadsheet = gc.open_by_key(sheet_id)
        _state = True
        logger.info("スプレッドシートに接続しました: %s", _spreadsheet.title)
    except Exception as e:  # noqa: BLE001
        logger.exception("スプレッドシート接続に失敗。スタブ動作にフォールバック: %s", e)
        _spreadsheet = None
        _state = False
    return _spreadsheet


def _ws(name: str):
    """ワークシートを取得する。

    - 無ければヘッダー付きで新規作成する。
    - 既存でも1行目が空（ヘッダー消失）なら自動でヘッダーを書き直す（自己修復）。
    確認はプロセス内で各シート1回だけ行う。無効時は None。
    """
    ss = _connect()
    if ss is None:
        return None
    header = WORKSHEETS.get(name, [])
    try:
        import gspread

        try:
            ws = ss.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=name, rows=1000, cols=max(2, len(header)))
            if header:
                ws.append_row(header, value_input_option="USER_ENTERED")
            _verified.add(name)
            logger.info("ワークシートを新規作成: %s", name)
            return ws

        # 既存シート: ヘッダーが消えていれば補う（1プロセス1回）
        if header and name not in _verified:
            first = ws.row_values(1)
            if not any(str(c).strip() for c in first):
                ws.update(values=[header], range_name="A1",
                          value_input_option="USER_ENTERED")
                logger.info("ワークシートのヘッダーを修復: %s", name)
            _verified.add(name)
        return ws
    except Exception as e:  # noqa: BLE001
        logger.exception("ワークシート取得に失敗 (%s): %s", name, e)
        return None


def _append(name: str, row: list) -> bool:
    ws = _ws(name)
    if ws is None:
        return False
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("行の追加に失敗 (%s): %s", name, e)
        return False


def _records(name: str) -> list[dict]:
    ws = _ws(name)
    if ws is None:
        return []
    try:
        return ws.get_all_records()
    except Exception as e:  # noqa: BLE001
        logger.exception("レコード取得に失敗 (%s): %s", name, e)
        return []


def _find_row(sheet_name: str, col: int, value: str) -> Optional[int]:
    """指定列(1始まり)の値が一致する最初の行番号(1始まり)を返す。ヘッダー行は除外。"""
    ws = _ws(sheet_name)
    if ws is None:
        return None
    try:
        values = ws.col_values(col)
    except Exception as e:  # noqa: BLE001
        logger.exception("列取得に失敗 (%s, col=%s): %s", sheet_name, col, e)
        return None
    target = str(value).strip()
    for i, v in enumerate(values):
        if i == 0:  # ヘッダー行
            continue
        if str(v).strip() == target:
            return i + 1  # 1始まりの行番号
    return None


def _update_row(sheet_name: str, row: int, start_col_letter: str, cells: list) -> bool:
    """指定行の start_col_letter から横方向に cells を書き込む。"""
    ws = _ws(sheet_name)
    if ws is None:
        return False
    end_col_letter = chr(ord(start_col_letter) + len(cells) - 1)
    rng = f"{start_col_letter}{row}:{end_col_letter}{row}"
    try:
        ws.update(values=[cells], range_name=rng, value_input_option="USER_ENTERED")
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("行更新に失敗 (%s, %s): %s", sheet_name, rng, e)
        return False


def _delete_row(sheet_name: str, row: int) -> bool:
    ws = _ws(sheet_name)
    if ws is None:
        return False
    try:
        ws.delete_rows(row)
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("行削除に失敗 (%s, row=%s): %s", sheet_name, row, e)
        return False


def _month_range(year: int, month: int) -> tuple[date, date]:
    """指定年月の初日と末日(date)を返す。"""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _rows_by_period(sheet_name: str, start: date, end: date) -> list[list]:
    """日付列(A列)が [start, end] に入る全行を返す（ヘッダー除外）。"""
    ws = _ws(sheet_name)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:  # noqa: BLE001
        logger.exception("行取得に失敗 (%s): %s", sheet_name, e)
        return []
    result = []
    for i, row in enumerate(values):
        if i == 0:  # ヘッダー
            continue
        d = _parse_date(row[0] if row else "")
        if d and start <= d <= end:
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# 家計簿シート
# ---------------------------------------------------------------------------
def add_expense(amount: int, genre: str = "", note: str = "",
                when: Optional[datetime] = None) -> None:
    if not _append(WS_EXPENSE, [_date(when), amount, genre, note]):
        logger.info("[STUB] 家計簿を記録: %d円 / %s / %s", amount, genre, note)


def find_expense_row(date: str, description: str) -> Optional[int]:
    """日付(A列)と備考(D列)の両方が一致する支出行を特定する。"""
    ws = _ws(WS_EXPENSE)
    if ws is None:
        return None
    try:
        values = ws.get_all_values()
    except Exception as e:  # noqa: BLE001
        logger.exception("家計簿の取得に失敗: %s", e)
        return None
    d = str(date).strip()
    note = str(description).strip()
    for i, row in enumerate(values):
        if i == 0:  # ヘッダー
            continue
        row_date = row[0].strip() if len(row) > 0 else ""
        row_note = row[3].strip() if len(row) > 3 else ""
        if row_date == d and row_note == note:
            return i + 1
    return None


def update_expense_row(row: int, amount: int, description: str, category: str) -> None:
    """支出の B:金額 / C:ジャンル / D:備考 を更新する。"""
    if not _update_row(WS_EXPENSE, row, "B", [amount, category, description]):
        logger.info("[STUB] 支出を更新: row=%s %d円 / %s / %s", row, amount, category, description)


def delete_expense_row(row: int) -> None:
    if not _delete_row(WS_EXPENSE, row):
        logger.info("[STUB] 支出を削除: row=%s", row)


# ---------------------------------------------------------------------------
# 体重シート
# ---------------------------------------------------------------------------
def add_weight(value: float, when: Optional[datetime] = None) -> None:
    if not _append(WS_WEIGHT, [_date(when), value]):
        logger.info("[STUB] 体重を記録: %.1fkg", value)


# ---------------------------------------------------------------------------
# 日記シート
# ---------------------------------------------------------------------------
def add_diary(text: str, when: Optional[datetime] = None) -> None:
    if not _append(WS_DIARY, [_date(when), text]):
        logger.info("[STUB] 日記を記録: %s", text[:50])


# ---------------------------------------------------------------------------
# タスクシート
# ---------------------------------------------------------------------------
def add_task(task: str, scheduled_date: str = "", when: Optional[datetime] = None) -> None:
    """タスクを追加する。

    scheduled_date が空 ＝ バックログ（実行日未定）。
    完了列は空（未完了）で追加する。
    """
    if not _append(WS_TASK, [_date(when), task, "", scheduled_date]):
        logger.info("[STUB] タスクを追加: %s (実行予定日=%s)", task, scheduled_date or "バックログ")


def get_incomplete_tasks() -> list[str]:
    """実行予定日が「今日以前（予定日 ≤ 今日）」で未完了のタスクの「内容」一覧を返す。

    予定日が空（バックログ）や、未来日のタスクは含まない。
    過去日のやり残しも取りこぼさないよう「今日ちょうど」ではなく「以前」で判定する。
    """
    today = date.today()
    tasks = []
    for r in _records(WS_TASK):
        content = str(r.get("内容", "")).strip()
        if not content or _is_done(r.get("完了", "")):
            continue
        scheduled = _parse_date(r.get("実行予定日"))
        if scheduled is not None and scheduled <= today:
            tasks.append(content)
    return tasks


def get_backlog_tasks() -> list[dict]:
    """実行予定日が空で未完了のタスクを行番号付きで返す。

    戻り値: [{"row": 行番号, "content": タスク内容}, ...]
    行番号はスプレッドシート上の実際の行（1始まり・ヘッダーが1行目）。
    """
    result: list[dict] = []
    # get_all_records はヘッダー(1行目)を除くので、レコード index i のシート行は i + 2
    for i, r in enumerate(_records(WS_TASK)):
        content = str(r.get("内容", "")).strip()
        scheduled = str(r.get("実行予定日", "")).strip()
        if content and not scheduled and not _is_done(r.get("完了", "")):
            result.append({"row": i + 2, "content": content})
    return result


def schedule_tasks(row_numbers: list[int], scheduled_date: str) -> None:
    """指定した行番号のタスクの D 列（実行予定日）に日付をセットする。"""
    ws = _ws(WS_TASK)
    if ws is None:
        logger.info("[STUB] 実行予定日を設定: rows=%s date=%s", row_numbers, scheduled_date)
        return
    for row in row_numbers:
        try:
            ws.update_cell(row, 4, scheduled_date)  # D列（実行予定日）= 4列目
        except Exception as e:  # noqa: BLE001
            logger.exception("実行予定日の設定に失敗 (row=%s): %s", row, e)


def complete_task_by_content(content: str) -> bool:
    """内容(B列)でタスクを検索し、完了(C列)を TRUE にする。成功なら True。"""
    row = _find_row(WS_TASK, 2, content)
    if row is None:
        return False
    ws = _ws(WS_TASK)
    if ws is None:
        return False
    try:
        ws.update_cell(row, 3, "TRUE")  # C列（完了）
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("タスク完了の更新に失敗 (row=%s): %s", row, e)
        return False


def update_task_by_content(old_content: str, new_content: str) -> bool:
    """内容(B列)が old_content のタスクを new_content に更新する。成功なら True。"""
    row = _find_row(WS_TASK, 2, old_content)
    if row is None:
        return False
    return _update_row(WS_TASK, row, "B", [new_content])


def delete_task_by_content(content: str) -> bool:
    """内容(B列)が一致するタスクを削除する。成功なら True。"""
    row = _find_row(WS_TASK, 2, content)
    if row is None:
        return False
    return _delete_row(WS_TASK, row)


# ---------------------------------------------------------------------------
# アイデアシート
# ---------------------------------------------------------------------------
def add_idea(title: str, category: str, content: str,
             when: Optional[datetime] = None) -> None:
    if not _append(WS_IDEA, [_date(when), title, category, content]):
        logger.info("[STUB] アイデアを記録: %s [%s]", title, category)


def find_idea_row(title: str) -> Optional[int]:
    """タイトル(B列)が一致するアイデア行を特定する。"""
    return _find_row(WS_IDEA, 2, title)


def update_idea_row(row: int, title: str, category: str, content: str) -> None:
    """アイデアの B:タイトル / C:カテゴリ / D:内容 を更新する。"""
    if not _update_row(WS_IDEA, row, "B", [title, category, content]):
        logger.info("[STUB] アイデアを更新: row=%s %s [%s]", row, title, category)


# ---------------------------------------------------------------------------
# 記事シート
# ---------------------------------------------------------------------------
def add_article(url: str, summary: str, when: Optional[datetime] = None) -> None:
    if not _append(WS_ARTICLE, [_date(when), url, summary]):
        logger.info("[STUB] 記事を記録: %s", url)


# ---------------------------------------------------------------------------
# 直近データの取得（日付は "-" / "/" 両形式に対応してソート）
# ---------------------------------------------------------------------------
def _get_recent(name: str, limit: int, date_col: str = "日付") -> list[dict]:
    """指定シートのレコードを日付昇順に並べ、直近 limit 件を返す。

    日付は _parse_date で "%Y-%m-%d" / "%Y/%m/%d" 両対応。
    パースできない行は最古として扱う（先頭側に寄せる）。
    """
    rows = _records(name)
    rows.sort(key=lambda r: _parse_date(r.get(date_col)) or date.min)
    return rows[-limit:] if limit > 0 else rows


def get_recent_expenses(limit: int = 30) -> list[dict]:
    """家計簿の直近データを返す（日付昇順、末尾が最新）。"""
    return _get_recent(WS_EXPENSE, limit)


def get_recent_weights(limit: int = 30) -> list[dict]:
    """体重の直近データを返す（日付昇順、末尾が最新）。"""
    return _get_recent(WS_WEIGHT, limit)


def get_recent_diary(limit: int = 30) -> list[dict]:
    """日記の直近データを返す（日付昇順、末尾が最新）。"""
    return _get_recent(WS_DIARY, limit)


def get_recent_ideas(limit: int = 20) -> list[dict]:
    """アイデアの直近データを返す（日付・タイトル・カテゴリ・内容、日付昇順）。"""
    return _get_recent(WS_IDEA, limit)


# ---------------------------------------------------------------------------
# 月次集計（月末レポート用）
# ---------------------------------------------------------------------------
def get_monthly_weight_change(year: int, month: int) -> tuple[Optional[float], Optional[float]]:
    """指定月の最初と最後の体重を (first, last) で返す。記録が無ければ (None, None)。"""
    start, end = _month_range(year, month)
    rows = _rows_by_period(WS_WEIGHT, start, end)
    if not rows:
        return None, None
    rows.sort(key=lambda r: _parse_date(r[0]) or date.min)

    def _val(r):
        try:
            return float(r[1])
        except (IndexError, ValueError):
            return None

    return _val(rows[0]), _val(rows[-1])


def get_monthly_expense_total(year: int, month: int) -> int:
    """指定月の支出（金額・B列）の合計を返す。"""
    start, end = _month_range(year, month)
    total = 0
    for row in _rows_by_period(WS_EXPENSE, start, end):
        try:
            total += int(str(row[1]).replace(",", "").strip())
        except (IndexError, ValueError):
            continue
    return total


def get_monthly_count(sheet_name: str, year: int, month: int) -> int:
    """指定月のシートの記録数（行数）を返す。"""
    start, end = _month_range(year, month)
    return len(_rows_by_period(sheet_name, start, end))


def get_completed_tasks_monthly(year: int, month: int) -> int:
    """指定月（日付・A列ベース）の完了タスク数を返す。"""
    start, end = _month_range(year, month)
    count = 0
    for row in _rows_by_period(WS_TASK, start, end):
        done = row[2] if len(row) > 2 else ""
        if _is_done(done):
            count += 1
    return count


def get_diary_by_period(start: date, end: date) -> list[dict]:
    """期間内の日記を [{"date": ..., "content": ...}, ...] で返す。"""
    result = []
    for row in _rows_by_period(WS_DIARY, start, end):
        result.append({
            "date": row[0] if row else "",
            "content": row[1] if len(row) > 1 else "",
        })
    return result


# ---------------------------------------------------------------------------
# AI秘書用：データ取得
# ---------------------------------------------------------------------------
def dump_for_assistant(limit_per_sheet: int = 50) -> str:
    """各シートの直近データをまとめたテキストを返す（AI秘書のコンテキスト用）。"""
    if _connect() is None:
        return "（スプレッドシート未連携のため、まだデータはありません）"

    parts: list[str] = []
    for name in WORKSHEETS:
        rows = _records(name)
        if not rows:
            continue
        recent = rows[-limit_per_sheet:]
        header = WORKSHEETS[name]
        lines = [" / ".join(header)]
        for r in recent:
            lines.append(" / ".join(str(r.get(col, "")) for col in header))
        parts.append(f"# {name}\n" + "\n".join(lines))

    if not parts:
        return "（スプレッドシートにまだデータがありません）"
    return "\n\n".join(parts)
