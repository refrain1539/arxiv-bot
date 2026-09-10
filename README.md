# arxiv-bot

Ubuntuのsystemd timerで毎日09:23, 10:23, 12:23, 14:23, 16:23 JSTに arXiv の新着論文(hep-th中心)を取得し、
Gemini APIで興味に合うものだけを選別・日本語訳してDiscordへ通知するBotです。
金銭コストはかかりません(すべて無料枠の範囲で動作します)。

## 本番運用

定期実行と本番の手動実行はUbuntuのsystemd service/timerで行う。GitHub Actionsの
workflow_dispatchは障害復旧・DRY_RUN確認用に残しているが、移行後の本番手動実行には
使用しない。運用コマンドと更新・復元手順は
[`docs/ubuntu-operation.md`](docs/ubuntu-operation.md)を参照。

## できること

- arXiv (hep-th / gr-qc) の新着論文を毎日チェック(クロスリストされた quant-ph 論文も拾われます)
- Gemini APIが研究プロファイルに沿って関連度をスコア付け(0-10点)
- 論文を4段階(must_read / worth_reading / abstract_only / ignore)に分類し、
  段階に応じた形式でDiscordに通知(アブストラクトは日本語訳つき、1論文1メッセージ)
- 著者ウォッチリストに登録した著者の新着論文は、スコアに関係なく必ず通知(🔔著者アラート)
- must_read論文にはBibTeX entryを自動生成し、Issueに併記
- Discordのリアクション(👍/👎)またはGitHub Issueのコメントで「興味あり/なし」をフィードバック
  すると、翌日以降の判定精度に反映される
- Discordで📖を押すと、その論文のPDF全文をGeminiに渡して解説書(HTML)を生成し、返信で受け取れる

## 新機能の使い方

### 著者ウォッチリスト(watch_authors)

`config.yml` の `watch_authors` に監視したい著者の姓を追記すると、その著者が含まれる
新着論文はGeminiのスコアに関係なく必ず通知されます(Discord・Issueとも先頭に
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

`config.yml` の `notify_categories` で、Discordに通知するカテゴリを選べます
(GitHub Issueには `ignore` 以外の全カテゴリが常に記録されます)。

```yaml
notify_categories:
  - must_read
  - worth_reading
  - abstract_only
```

`notify_categories` に含めたカテゴリの論文は、それぞれ1論文1メッセージ・1embedで
Discordに投稿されます。`must_read` / `worth_reading` はスコア(★)・著者・理由・
アブストラクト全訳つき、`abstract_only` はタイトル和訳・スコア・一言要約のみです。
ただし `max_papers`(既定8)で1日あたりの通知件数には上限があります(詳しくは
「Discordの通知形式について」を参照)。`notify_categories` から外したカテゴリは
Discordには出ませんが、GitHub Issueには(`ignore`以外)常に記録されます。

### 研究プロファイル(data/my_profile.md)

`data/my_profile.md` を編集すると、Geminiの関連度判定がこのファイルの内容を最優先で
参照するようになります(`config.yml` の `interest_profile` はこのファイルが無い場合の
フォールバックです)。

編集方法: `data/my_profile.md` をテキストエディタで開き、`<...>` のプレースホルダー部分に
現在の研究テーマ・注目している論文・興味が薄い分野などを具体的に書き込んでください。
書けば書くほど判定精度が上がります(ただし先頭4000字を超えた部分は自動的に切り詰められます)。

## Discordの通知形式について

- 1論文 = 1メッセージ = 1embedで投稿されます
- 投稿後、Bot自身がそのメッセージに📖👍👎の3つのリアクションを付けます
- 👍/👎を押すと、`research-bots-arxiv-reactions.timer`(10:07〜23:52、および翌00:07〜01:52 JSTの間、15分おきに実行)
  がそれを回収して`data/feedback.json`に記録し、翌日以降のGemini判定に反映されます
- 📖を押すと、その論文のPDF全文をGeminiに渡して解説書(HTML)を生成し、元のメッセージへの
  返信としてファイル添付で返します
- `max_papers`(既定8)で頭打ちになるため、Discordに届くのは1日あたり8件までです
  (🔔著者アラートは上限の対象外で、あればその分が上乗せされます)。
  それ以外の論文(`ignore`以外の全カテゴリ、平均で1日29件ほど)はGitHub Issueに記録されるので、
  全体を眺めたいときはIssueを見る、という使い分けになります

## セットアップ手順

### 1. リポジトリを作成する

このフォルダの中身をそのまま新しい**パブリック**GitHubリポジトリにpushしてください
(GitHub Actionsの無料枠はパブリックリポジトリが対象です)。

### 2. Gemini APIキーを取得する

[Google AI Studio](https://aistudio.google.com/) で無料のAPIキーを発行します。

### 3. Discordを設定する

Discord Botの作成・招待・チャンネルID取得・GitHub Secretsへの登録までの詳しい手順は
[docs/discord.md](docs/discord.md) を参照してください。

### 4. GitHub Secretsに登録する

リポジトリの `Settings > Secrets and variables > Actions` から、以下を登録してください。

| Secret名 | 必須 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | ○ | Gemini APIキー |
| `GEMINI_MODEL` | - | モデル名を変更したい場合のみ(未設定なら `gemini-3.6-flash`) |
| `DISCORD_BOT_TOKEN` | ○ | Discord Bot Token(`docs/discord.md`参照) |
| `DISCORD_CHANNEL_ID` | ○ | 投稿先チャンネルのID(`docs/discord.md`参照) |

`GITHUB_TOKEN` は GitHub Actions が自動発行するため、登録不要です。

### 5. config.yml を編集する(任意)

`config.yml` に研究プロファイル・スコア閾値・著者ウォッチリスト(`watch_authors`)・
通知カテゴリ(`notify_categories`)などの設定があります。自分の興味に合わせて書き換えてください。
より詳しい研究プロファイルを設定したい場合は `data/my_profile.md` を編集してください
(上記「新機能の使い方」を参照)。

### 6. 動作確認

初回確認と本番手動実行はUbuntuのsystemd serviceを通して行う。GitHub Actionsの
workflow_dispatchは障害復旧・DRY_RUN確認専用であり、通常の本番起動には使わない。

## よくあるエラーと対処

- **`GEMINI_API_KEY`関連のエラー / 401**: Secretsに正しく登録されているか確認してください。キーが無効化・失効している場合は再発行してください。
- **Geminiから429 (Too Many Requests)**: 無料枠のレート制限(1日あたりのリクエスト数上限など)に
  達しています。論文は`gemini_batch_size`件ずつまとめて1リクエストで判定するため、通常はリクエスト
  数を抑えられますが、新着論文が非常に多い日などにまだ429が頻発する場合は、`config.yml`の
  `gemini_batch_size`をさらに大きくする(例: 12, 15)か、`lookback_hours`や`categories`を絞って
  1日あたりの判定件数自体を減らしてください。ログに`(3/3)`と出ている場合は3回リトライしても
  解消しなかったことを意味し、そのバッチの論文は`ignore`扱いになります(通知からは漏れますが、
  処理自体は止まりません)。
- **Discordで401 Unauthorized**: `DISCORD_BOT_TOKEN` が誤っているか失効しています。Developer Portalの「Bot」画面で「Reset Token」を行い、新しいTokenをGitHub Secretsに登録し直してください。
- **Discordで403 Forbidden**: Botに必要な権限(View Channel / Send Messages / Read Message History / Add Reactions)が付与されていないか、Botがそのチャンネルにアクセスできません。`docs/discord.md` の招待手順をやり直すか、チャンネルの権限設定を確認してください。
- **Discordで404 Not Found**: `DISCORD_CHANNEL_ID` が誤っています。`docs/discord.md` の手順でチャンネルIDを取り直し、Secretsを更新してください。
- **Issueが作成されない / closeされない**: `arxiv.env` の `GITHUB_TOKEN` に対象リポジトリのIssues読書き権限があるか、journalのログを確認してください。
- **timerが動かない**: `systemctl list-timers --all 'research-bots-*'` と `journalctl -u research-bots-arxiv-daily.service` を確認してください。

## ディレクトリ構成

```
arxiv-bot/
├── .github/workflows/
│   ├── daily.yml                 # 障害復旧・DRY_RUN用の手動workflow
│   └── reactions.yml             # 障害復旧・DRY_RUN用の手動workflow
├── src/
│   ├── main.py                   # エントリポイント
│   ├── arxiv_fetch.py            # arXiv API から新着取得・著者ウォッチリスト照合
│   ├── judge_translate.py        # Gemini で4段階分類+翻訳(研究プロファイル注入)
│   ├── bibtex.py                 # must_read論文のBibTeX entry組み立て
│   ├── feedback.py               # GitHub Issue からフィードバック回収・反映
│   ├── notify.py                 # LINE送信 / メール送信(現在は未使用。Discord移行前の実装)
│   ├── notify_discord.py         # Discordへの通知(1論文1メッセージ・1embed、投稿後に📖👍👎を付与)
│   ├── post_log.py               # posted_messages.json(投稿したmessage_idの記録)の読み書き
│   ├── reactions.py              # Discordのリアクション(👍👎)を回収してfeedback.jsonに反映
│   └── explain.py                # 📖リクエストに対し、PDF全文をGeminiに渡して解説書(HTML)を生成
├── data/
│   ├── seen_ids.json             # 通知済み論文ID(重複防止)
│   ├── feedback.json             # 蓄積されたフィードバック
│   ├── posted_messages.json      # Discordに投稿したmessage_idの記録
│   └── my_profile.md             # 研究プロファイル(ユーザーが編集)
├── docs/
│   └── discord.md                # Discordの設定手順(Bot作成〜Secrets登録)
├── tests/                        # ユニットテスト(unittest)
├── config.yml                    # ユーザーが編集する設定ファイル
├── requirements.txt
└── README.md
```
