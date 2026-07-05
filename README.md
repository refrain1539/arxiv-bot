# arxiv-bot

毎朝7:00 JST(GitHub Actionsの仕様上、数十分遅延することがあります)に arXiv の新着論文(hep-th中心)を取得し、
Gemini APIで興味に合うものだけを選別・日本語訳し、LINEまたはメールで通知するBotです。
金銭コストはかかりません(すべて無料枠の範囲で動作します)。

## できること

- arXiv (hep-th / gr-qc / quant-ph) の新着論文を毎朝チェック
- Gemini APIが興味プロファイルに沿って関連度をスコア付け(0-10点)
- 閾値以上の論文だけをLINE・メールで通知(アブストラクトは日本語訳つき)
- GitHub Issueのコメントで「興味あり/なし」をフィードバックすると、翌朝以降の判定精度に反映される

## セットアップ手順

### 1. リポジトリを作成する

このフォルダの中身をそのまま新しい**パブリック**GitHubリポジトリにpushしてください
(GitHub Actionsの無料枠はパブリックリポジトリが対象です)。

### 2. Gemini APIキーを取得する

[Google AI Studio](https://aistudio.google.com/) で無料のAPIキーを発行します。

### 3. LINE Messaging APIを設定する(第1優先・任意)

1. [LINE Developers](https://developers.line.biz/) でMessaging APIチャネルを作成(無料の「Developer Trial」または公式アカウントの無料プランでOK)
2. チャネルアクセストークン(長期)を発行 → `LINE_CHANNEL_ACCESS_TOKEN`
3. 自分のLINEユーザーID(botを友だち追加した上で確認)→ `LINE_USER_ID`
4. 月200通までは無料(このBotは1日1通しか送らないので十分収まります)

### 4. Gmail SMTPを設定する(第2優先・フォールバック、任意だがLINEを使わないなら必須)

1. Googleアカウントで2段階認証を有効化
2. [アプリパスワード](https://myaccount.google.com/apppasswords) を発行
3. `GMAIL_ADDRESS`(送信元アドレス)、`GMAIL_APP_PASSWORD`(発行したアプリパスワード)、`MAIL_TO`(送り先アドレス)を用意

LINE・メールのどちらか一方は必ず設定してください(両方未設定だと通知先がなくなります。その場合もGitHub Issueは作成されます)。

### 5. GitHub Secretsに登録する

リポジトリの `Settings > Secrets and variables > Actions` から、以下を登録してください。

| Secret名 | 必須 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | ○ | Gemini APIキー |
| `GEMINI_MODEL` | - | モデル名を変更したい場合のみ(未設定なら `gemini-2.5-flash`) |
| `LINE_CHANNEL_ACCESS_TOKEN` | △ | LINE通知を使う場合 |
| `LINE_USER_ID` | △ | LINE通知を使う場合 |
| `GMAIL_ADDRESS` | △ | メール通知を使う場合 |
| `GMAIL_APP_PASSWORD` | △ | メール通知を使う場合 |
| `MAIL_TO` | △ | メール通知を使う場合 |

`GITHUB_TOKEN` は GitHub Actions が自動発行するため、登録不要です。

### 6. config.yml を編集する(任意)

`config.yml` に興味プロファイルやスコア閾値などの設定があります。自分の興味に合わせて書き換えてください。

### 7. 動作確認

リポジトリの「Actions」タブ → 「daily-arxiv」→「Run workflow」で手動実行できます。
初回は `dry_run: true` を指定すると、通知やIssue作成・状態ファイルの保存を行わずログだけで動作確認できます。

## よくあるエラーと対処

- **`GEMINI_API_KEY`関連のエラー / 401**: Secretsに正しく登録されているか確認してください。キーが無効化・失効している場合は再発行してください。
- **Geminiから429 (Too Many Requests)**: 無料枠のレート制限に達しています。コードは自動で指数バックオフ・リトライしますが、頻発する場合は取得件数(`max_results`)や実行頻度を見直してください。
- **LINEで401 Unauthorized**: `LINE_CHANNEL_ACCESS_TOKEN` の有効期限切れ、または誤ったトークンです。LINE Developersコンソールで再発行してください。
- **LINEでメッセージが届かない**: `LINE_USER_ID` が正しいか、Botを友だち追加しているか確認してください。
- **メールが届かない(SMTP認証エラー)**: 通常のGoogleパスワードではなく、必ず「アプリパスワード」を使ってください。2段階認証が有効になっている必要があります。
- **`git push`で失敗する**: ワークフローの `permissions: contents: write` が設定されているか確認してください(このリポジトリでは設定済みです)。組織のリポジトリルールでActionsのpushが制限されている場合は、リポジトリ設定を見直してください。
- **Issueが作成されない / closeされない**: `permissions: issues: write` が必要です(設定済み)。それでも失敗する場合はActionsのログを確認してください。
- **スケジュール実行が7:00 JSTちょうどに来ない**: GitHub Actionsの `schedule` は仕様上、数十分〜1時間程度遅延することがあります。仕様であり、Bot側の不具合ではありません。

## ディレクトリ構成

```
arxiv-bot/
├── .github/workflows/daily.yml   # 毎朝の定期実行ワークフロー
├── src/
│   ├── main.py                   # エントリポイント
│   ├── arxiv_fetch.py            # arXiv API から新着取得
│   ├── judge_translate.py        # Gemini で選別+翻訳
│   ├── feedback.py               # GitHub Issue からフィードバック回収・反映
│   └── notify.py                 # LINE送信 / メール送信
├── data/
│   ├── seen_ids.json             # 通知済み論文ID(重複防止)
│   └── feedback.json             # 蓄積されたフィードバック
├── config.yml                    # ユーザーが編集する設定ファイル
├── requirements.txt
└── README.md
```
