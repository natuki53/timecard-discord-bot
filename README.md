# 🕒 Timecard Discord Bot

**Timecard Discord Bot** は、Discord サーバーでメンバーの出勤・退勤・休憩時間を記録し、月ごとの勤務時間を集計できるボットです。  
出勤・退勤・休憩をスラッシュコマンドで簡単に管理し、SQLite データベースに記録します。

---

## 📦 主な機能

- `/start`：出勤開始
- `/end`：退勤（勤務時間を計算）
- `/break`：休憩開始
- `/restart`：休憩終了
- `/monthly`：今月の合計勤務時間を表示
- `/last_monthly`：先月の合計勤務時間を表示

---

## 🛠️ 使い方

### 1️⃣ 環境構築

#### 必要なもの

- Python 3.9 以上
- 依存パッケージ（`pip install -r requirements.txt`）
  - `discord.py`
  - `aiosqlite`
  - `python-dotenv`
- `.env` ファイル

#### `.env` の作成

以下の内容を `.env` ファイルとして用意してください。

```env
DB_DIR=データベースを保存するディレクトリのパス
DISCORD_TOKEN=DiscordのBotトークン
```

### 2️⃣ 実行

まず、ターミナルやコマンドプロンプトで、Botのフォルダに移動します。

```bash
cd フォルダのパス
```

移動後、以下のコマンドでボットを起動します。

```
python timecard-main.py
```

また、毎回コマンドを入力するのが面倒な場合は、起動用の`.batファイル`を作成することをおすすめします。

```
@echo off
cd /d C:\Users\YourName\github\timecard-discord-bot
python timecard-main.py
pause
```

`.bat`ファイルの内容を上記のように設定し、ダブルクリックで起動することで、
自動でディレクトリ移動 → Bot起動まで実行できます。

※`cd /d`のパス部分は、自分のBotのフォルダパスに合わせてください。

### Docker で実行（サーバー向け）

```bash
cp .env.example .env   # 初回のみ。トークン等を編集
docker compose up -d --build
```

`db/` フォルダは Git 管理外です。コンテナ再作成・自動デプロイでも DB データは保持されます。

### 自動デプロイ（main 更新時）

GitHub の **push webhook** で main 更新を検知し、即座に再ビルド・再起動します。

| 項目 | 値 |
|------|-----|
| Webhook URL | `https://mu-natuki.com/hooks/deploy-timecard-bot` |
| イベント | `push`（`refs/heads/main` のみ） |
| リポジトリ | `natuki53/timecard-discord-bot` |

サーバー側では既存の [adnanh/webhook](https://github.com/adnanh/webhook) コンテナ（`deploy`）がフックを受信し、`scripts/deploy.sh` を実行します。`db/` と `.env` は Git 管理外のため上書きされません。

手動デプロイ: `./scripts/deploy.sh`

---

## 💾 データ管理

- 勤務データは **月ごとの SQLite データベースファイル** に保存されます。
- 出勤中の状態は `active_sessions.db` に保存されます（月をまたいでも退勤可能）。
- 出勤・退勤・休憩はサーバーID + ユーザーIDごとに管理されます。
- **旧バージョンのDB**（`users` テーブル・`guild_id` なし履歴）も起動時に自動移行されます。
- 初回コマンド実行時、旧データは**そのサーバーのIDに一括紐付け**されます（1サーバー運用向け）。
- データは `DB_DIR` で指定したフォルダ内に `work_tracking_YYYY_MM.db` として保存されます。
- 月ごとの勤務履歴はテーブル名 `history_YYYY_MM` に記録されます。

---

## 🚀 使用例

| コマンド | 説明 |
| -------- | ---- |
| `/start` | 出勤する |
| `/break` | 休憩を開始する |
| `/restart` | 休憩を終了する |
| `/end` | 退勤する |
| `/monthly` | 今月の合計勤務時間を確認する |
| `/last_monthly` | 先月の合計勤務時間を確認する |

---

## 🌸 開発者向け補足

- **コアファイル**: `timecard-main.py`
- データベース構成はSQLite（非同期: aiosqlite）で、サーバー・ユーザーごと、月ごとの履歴を管理。
- 環境変数管理は `python-dotenv` を使用。

---

## 📖 ライセンス

MIT License（予定）

---

## 🙏 貢献

改善提案やバグ報告はプルリクエストまたはIssueでお願いします！  
作成者: [natuki53](https://github.com/natuki53)

---

## 🌟 今後の改善予定

- 勤務時間の月別レポート出力
- ボットの多言語対応
- 勤務開始・終了時のリマインダー機能
