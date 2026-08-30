# Discord通知の設定手順

arxiv-botの通知先をLINEからDiscordに切り替えるための、手作業での準備手順です。
Bot本体の実装(`src/notify_discord.py`など)は別途進んでいる前提で、ここでは
Discord側の設定とGitHub Secretsへの登録のみを扱います。

この方式はDiscord Bot TokenによるREST API送信のみを使います。Gateway(WebSocket)への
常時接続は行わないため、GitHub Actions上でcron実行するだけで動作します。常駐サーバーを
用意する必要はありません。

通知は1論文につき1メッセージ・1embedで送られ、投稿後にBotが自動で📖👍👎の3つの
リアクションを付けます。ユーザーが👍または👎を押すと、別ワークフロー
(`.github/workflows/reactions.yml`、日中15分おきに実行)がそれを回収して`data/feedback.json`に
記録し、翌朝以降のGemini判定に反映されます。📖は「この論文の解説書がほしい」という
意思表示用のリアクションですが、解説書を実際に生成する機能自体は別タスクでまだ
実装されていません。

必要なGitHub Secretsは次の2つです。

- `DISCORD_BOT_TOKEN`
- `DISCORD_CHANNEL_ID`

以下、上から順に進めてください。

## 1. Discord Developer PortalでApplicationを作成する

[Discord Developer Portal](https://discord.com/developers/applications) を開き、
右上の「New Application」をクリックします。任意の名前(例: `arxiv-bot`)を入力して
「Create」を押してください。

> **こうなればOK**: 作成したApplication名がページ上部に表示され、左側に
> General Information / Bot / OAuth2 などのメニューが並んだ設定画面に移動します。

## 2. Botを作成してTokenを発行する

左メニューの「Bot」を開き、「Reset Token」(初回は「Add Bot」の場合もあります)を
クリックします。確認ダイアログが出たら進めてください。表示された「Token」欄の
「Copy」ボタンでTokenをコピーし、あとで使うまで安全な場所に一時保存してください。

Tokenはこの画面でしか表示されません。閉じてしまったり見失ったりした場合は、
再度「Reset Token」で新しいTokenを発行する必要があります(古いTokenは無効になります)。

> **こうなればOK**: 「Token」欄に伏せ字ではない実際のTokenの文字列が表示され、
> 「Copy」ボタンでコピーできる状態になります。

同じ「Bot」画面にある「Privileged Gateway Intents」(MESSAGE CONTENT INTENTなど)は
**すべてOFFのままで構いません**。今回の実装はREST APIでの送信のみでGatewayに
接続しないため、これらのIntentは不要です。

> **こうなればOK**: 「PRESENCE INTENT」「SERVER MEMBERS INTENT」
> 「MESSAGE CONTENT INTENT」の3つのトグルがいずれもOFF(グレー)のままになっています。

## 3. 自分専用のサーバー(ギルド)を作成する

Discordアプリ(またはブラウザ版)で、左端の「+」アイコンから「サーバーを作成」を選び、
「オリジナルの作成」→ 任意の名前(例: `arxiv-bot通知用`)を入力してサーバーを作成します。
通知を受け取るためだけのプライベートなサーバーで構いません。

> **こうなればOK**: 左端のサーバー一覧に新しいサーバーのアイコンが追加され、
> クリックするとテキストチャンネル(例: `#general`)が表示されます。

## 4. OAuth2 URL GeneratorでBotの招待URLを作る

Developer Portalの左メニューから「OAuth2」→「URL Generator」を開きます。
「SCOPES」で `bot` にチェックを入れます。

> **こうなればOK**: チェックを入れると、その下に「BOT PERMISSIONS」という
> セクションが新しく表示されます。

続けて「BOT PERMISSIONS」で以下の4つだけにチェックを入れてください。

- View Channel
- Send Messages
- Read Message History
- Add Reactions

必要な権限は「メッセージを送る」「あとから読む」「リアクションを付ける」ための
最小限にとどめています。管理者権限やメッセージ削除・チャンネル管理などの権限は
不要なので、チェックを入れないでください。

> **こうなればOK**: ページ下部に生成されたURL(`https://discord.com/oauth2/authorize?...`)
> が表示され、「Copy」ボタンでコピーできる状態になっています。

コピーしたURLをブラウザで開きます。

> **こうなればOK**: 「どのサーバーに追加しますか?」という画面が表示され、
> ドロップダウンで手順3で作成した自分専用サーバーを選べる状態になります。

サーバーを選択して「認可」をクリックします。

> **こうなればOK**: 「〇〇が認証されました」という完了画面が表示され、
> サーバーのメンバー一覧にBotが追加されています。

## 5. 通知先チャンネルのIDを調べる

Discordの「ユーザー設定」(左下の歯車アイコン)→「詳細設定」を開き、
「開発者モード」をONにします。

> **こうなればOK**: 「開発者モード」のトグルがON(色が付いた状態)になります。

通知を送りたいチャンネルを右クリックし、「チャンネルIDをコピー」を選択します。

> **こうなればOK**: 右クリックメニューの一番下あたりに「チャンネルIDをコピー」が
> 表示され、クリックすると数字の羅列(チャンネルID)がクリップボードにコピーされます。

## 6. GitHub SecretsにTokenとチャンネルIDを登録する

このリポジトリの「Settings」タブ→左メニューの「Secrets and variables」→
「Actions」を開きます。

> **こうなればOK**: 「Repository secrets」という見出しの下に
> 「New repository secret」ボタンが表示されます。

「New repository secret」をクリックし、Name欄に `DISCORD_BOT_TOKEN` を入力、
Secret欄に手順2でコピーしたTokenを貼り付けて「Add secret」を押します。

> **こうなればOK**: Secrets一覧に `DISCORD_BOT_TOKEN` が追加され、
> 値は表示されずマスクされた状態になります。

同様にもう一度「New repository secret」をクリックし、Name欄に
`DISCORD_CHANNEL_ID` を入力、Secret欄に手順5でコピーしたチャンネルIDを貼り付けて
「Add secret」を押します。

> **こうなればOK**: Secrets一覧に `DISCORD_BOT_TOKEN` と `DISCORD_CHANNEL_ID` の
> 2つが並んで表示されます。

## 7. 動作確認

> **先に確認**: arXiv は土日にアナウンスを行わないため、土曜・日曜に実行しても
> 新着論文は0件になり、通知処理そのものが走りません(`config.yml` の
> `notify_when_empty: false` のため)。Discordへの投稿まで確認したい場合は、
> 平日、それも月曜以降の朝に実行してください。

リポジトリの「Actions」タブを開き、対象のワークフロー(daily-arxiv)を選んで
「Run workflow」をクリックします。`dry_run` に `true` を指定して実行してください。

> **こうなればOK**: ワークフローが緑のチェックマークで完了し、ログにDiscordへの
> 送信内容(またはDRY_RUNである旨)が出力されますが、実際のDiscordチャンネルには
> メッセージが投稿されません。

ログの内容に問題がなければ、もう一度「Run workflow」を実行し、今度は `dry_run` を
`false`(またはチェックを外した状態)にして実行します。

> **こうなればOK**: 手順3で作成したサーバーのチャンネルに実際にembed付きの
> メッセージが投稿され、投稿されたメッセージに📖👍👎の3つのリアクションが
> Botによって自動的に付いた状態になります。

👍または👎を実際に押してみて、`.github/workflows/reactions.yml`
(Actionsタブ上の表示名は `discord-reactions`)が動くのを待つか、同様に
「Actions」タブから手動実行して、`data/feedback.json` に反映されることを
確認してください。このワークフローは 10:00〜翌02:00 JST の間、15分おきに走ります。

> **こうなればOK**: `data/feedback.json` に、リアクションを押した論文のIDと
> 👍/👎の情報を含むエントリが追加されています。

回収対象は `data/posted_messages.json` に記録された**過去3日以内**の投稿です。
それより古い投稿に押しても拾われないので、リアクションは3日以内に付けてください。

一度処理したリアクションは `posted_messages.json` の `reactions_done` に記録され、
以降は問い合わせも行いません。そのため、あとから押し直して評価を変えることは
できません(評価を変えたい場合は `data/feedback.json` を直接編集してください)。

## トラブルシューティング

- **401 Unauthorized**: `DISCORD_BOT_TOKEN` が誤っているか失効しています。
  Developer Portalの「Bot」画面で「Reset Token」を行い、新しいTokenを
  GitHub Secretsに登録し直してください。
- **403 Forbidden**: Botに必要な権限(View Channel / Send Messages /
  Read Message History / Add Reactions)が付与されていないか、Botがその
  チャンネルにアクセスできません。手順4の招待をやり直すか、チャンネルの
  権限設定でBotのロールにアクセスを許可してください。
- **404 Not Found**: `DISCORD_CHANNEL_ID` が誤っています。手順5の方法で
  チャンネルIDを取り直し、Secretsを更新してください。
