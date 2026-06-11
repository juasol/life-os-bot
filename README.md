# life-os-bot

個人用の人生管理 Discord BOT。チャンネルごとに機能を分け、体重・タスク・振り返り・お金・アイデア・記事を管理し、AI秘書として質問にも答える。

Claude API（ビジョン / テキスト抽出 / 要約）を利用。スプレッドシート連携は `sheets.py` に切り出してあり、**現在はスタブ実装**（呼び出しをログ出力するだけ）。Discord 上で動く状態を先に作り、後から gspread での実装に差し替える。

## 機能とチャンネル

### BOT が自動で話しかけるチャンネル
| チャンネル名 | 動作 |
|---|---|
| `体重` | 毎朝7時に体重を尋ねる。数字 or 画像を受け取ると記録（画像は Claude ビジョンで数値読み取り）。 |
| `今日やること` | 毎朝7時に未完了タスクを一覧で送る。ユーザー入力は処理しない。 |
| `振り返り` | 毎夜22時に1日を尋ねる。返信を日記に記録し、明日のタスクを Claude で抽出して追加。 |

### ユーザーが自由に投げ込むチャンネル
| チャンネル名 | 動作 |
|---|---|
| `お金` | テキスト/画像から金額と内容を Claude で抽出し家計簿へ記録。 |
| `アイデア` | テキストをアイデアシートへ記録。 |
| `気になる記事` | URL のタイトル・本文を取得し Claude で要約して記録。 |
| `AI秘書` | スプレッドシートのデータを使って自由な質問に回答。 |

> チャンネルは **名前** で判別する。Discord サーバー側に上記と同じ名前のテキストチャンネルを作成しておくこと。

## セットアップ

```bash
# 1. 仮想環境（任意）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2. 依存インストール
pip install -r requirements.txt

# 3. 環境変数
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
#   .env を編集して DISCORD_TOKEN と ANTHROPIC_API_KEY を設定

# 4. 起動
python main.py
```

### Discord 側の設定
- Discord Developer Portal で BOT を作成しトークンを取得。
- **Privileged Gateway Intents** の **MESSAGE CONTENT INTENT** を ON にする（必須）。
- BOT をサーバーに招待し、上記の名前のチャンネルを用意する。

## スケジュール
- 朝の投稿: 毎日 **7:00 JST**（`体重` / `今日やること`）
- 夜の投稿: 毎日 **22:00 JST**（`振り返り`）

タイムゾーンは `Asia/Tokyo` 固定（`main.py` の `JST`）。

## スプレッドシート連携の有効化

`sheets.py` は gspread + サービスアカウントで実装済み。**認証情報（後述）と `GOOGLE_SHEET_ID` の両方が揃うと自動で本番動作**し、無ければログ出力だけのスタブ動作にフォールバックする（＝認証情報が無くても BOT は動く）。

サービスアカウント認証は次の2通りに対応（どちらか一方でよい）:
- **ローカル運用**: `credentials.json` をフォルダ直下に置く（`.gitignore` 済み）。
- **クラウド/GitHub運用**: `credentials.json` の中身(JSON)を環境変数 `GOOGLE_CREDENTIALS_JSON` に設定する（ファイル不要）。鍵をリポジトリに含めずデプロイできる。

有効化の手順:

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成。
2. **Google Sheets API** を有効化する（「APIとサービス」→「ライブラリ」）。
3. **サービスアカウント**を作成し、鍵を **JSON 形式**でダウンロード。
4. ダウンロードした鍵を `credentials.json` という名前でこのフォルダ直下に置く（`.gitignore` 済み）。
5. 連携したい Google スプレッドシートを開き、サービスアカウントのメールアドレス
   （`xxx@xxx.iam.gserviceaccount.com`）に **編集者権限で共有**する。
6. スプレッドシートの URL `.../d/<ここがID>/edit` の ID 部分を `.env` の `GOOGLE_SHEET_ID` に設定。
7. BOT を再起動。起動ログに「スプレッドシートに接続しました」と出れば成功。

ワークシート（タブ）は初回アクセス時に自動作成され、ヘッダーが消えても自動修復される。
日付は `YYYY-MM-DD`（ハイフン区切り）で統一。列構成は次のとおり:

| シート | 列 |
|---|---|
| 家計簿 | 日付 / 金額（円） / ジャンル / 備考 |
| 体重 | 日付 / 体重kg |
| 日記 | 日付 / 本文 |
| タスク | 日付 / 内容 / 完了 / 実行予定日 |
| アイデア | 日付 / タイトル / カテゴリ / 内容 |
| 記事 | 日付 / URL / 要約 |

タスクの「完了」列に `TRUE` / `完了` / `✓` などを入れると、朝の「今日やること」一覧から除外される。

## 今後の TODO
- [ ] 実際の `credentials.json` + `GOOGLE_SHEET_ID` を入れて本番接続を確認する。
- [ ] タスクの「完了」操作（現状は手動でシートの状態列を「完了」にする想定）。
