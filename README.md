# arxiv-bot

毎朝7:00 JST(GitHub Actionsの仕様上、数十分遅延することがあります)に arXiv の新着論文(hep-th中心)を取得し、
Gemini APIで興味に合うものだけを選別・日本語訳し、LINEまたはメールで通知するBotです。
金銭コストはかかりません(すべて無料枠の範囲で動作します)。

## できること

- arXiv (hep-th / gr-qc / quant-ph) の新着論文を毎朝チェック
- Gemini APIが研究プロファイルに沿って関連度をスコア付け(0-10点)
- 論文を4段階(must_read / worth_reading / abstract_only / ignore)に分類し、
  段階に応じた形式でLINE・メールに通知(アブストラクトは日本語訳つき)
- 著者ウォッチリストに登録した著者の新着論文は、スコアに関係なく必ず通知(🔔著者アラート)
- must_read論文にはBibTeX entryを自動生成し、Issueに併記
- GitHub Issueのコメントで「興味あり/なし」をフィードバックすると、翌朝以降の判定精度に反映される

## 新機能の使い方

### 著者ウォッチリスト(watch_authors)

`config.yml` の `watch_authors` に監視したい著者の姓を追記すると、その著者が含まれる
新着論文はGeminiのスコアに関係なく必ず通知されます(LINE・Issueとも先頭に
`🔔 著者アラート: <著者名>` と表示されます)。

```yaml
watch_authors:
  - "Takayanagi"
  - "Maldacena"
```

大文字小文字を区別しない部分一致なので、フルネームでなくても構いません。

### 4段階分類(notify_categories)

Geminiは各論文を以下の4段階に分類します。

| カテゴリ | 意味 |
|---|---|
| `must_read` | 現在の研究テーマに直接関係。当日中に読むべき |
| `worth_reading` | 関連分野で参考になる可能性がある。今週中に目を通す価値あり |
| `abstract_only` | 分野動向として要約だけ把握すれば十分 |
| `ignore` | 関連なし |

`config.yml` の `notify_categories` で、LINE・メールに通知するカテゴリを選べます
(GitHub Issueには `ignore` 以外の全カテゴリが常に記録されます)。

```yaml
notify_categories:
  - must_read
  - worth_reading
  - abstract_only
```

LINE・メールとも、`notify_categories` に含めたカテゴリは見出し(🔴must_read /
🟡worth_reading / ⚪abstract_only)ごとにまとめて全件表示されます。`must_read` /
`worth_reading` はスコア(★)・著者・理由・アブストラクト全訳つき、`abstract_only` は
タイトル和訳・スコア・一言要約のみです。`notify_categories` から外したカテゴリは
LINE・メールには出ませんが、GitHub Issueには(`ignore`以外)常に記録されます。

### 研究プロファイル(data/my_profile.md)

`data/my_profile.md` を編集すると、Geminiの関連度判定がこのファイルの内容を最優先で
参照するようになります(`config.yml` の `interest_profile` はこのファイルが無い場合の
フォールバックです)。

編集方法: `data/my_profile.md` をテキストエディタで開き、`<...>` のプレースホルダー部分に
現在の研究テーマ・注目している論文・興味が薄い分野などを具体的に書き込んでください。
書けば書くほど判定精度が上がります(ただし先頭4000字を超えた部分は自動的に切り詰められます)。

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

`config.yml` に研究プロファイル・スコア閾値・著者ウォッチリスト(`watch_authors`)・
通知カテゴリ(`notify_categories`)などの設定があります。自分の興味に合わせて書き換えてください。
より詳しい研究プロファイルを設定したい場合は `data/my_profile.md` を編集してください
(上記「新機能の使い方」を参照)。

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
│   ├── arxiv_fetch.py            # arXiv API から新着取得・著者ウォッチリスト照合
│   ├── judge_translate.py        # Gemini で4段階分類+翻訳(研究プロファイル注入)
│   ├── bibtex.py                 # must_read論文のBibTeX entry組み立て
│   ├── feedback.py               # GitHub Issue からフィードバック回収・反映
│   └── notify.py                 # LINE送信 / メール送信
├── data/
│   ├── seen_ids.json             # 通知済み論文ID(重複防止)
│   ├── feedback.json             # 蓄積されたフィードバック
│   └── my_profile.md             # 研究プロファイル(ユーザーが編集)
├── tests/                        # ユニットテスト(unittest)
├── config.yml                    # ユーザーが編集する設定ファイル
├── requirements.txt
└── README.md
```
