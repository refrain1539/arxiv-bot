# Ubuntu運用

本番のコードと状態は `/home/seiya/services/arxiv-bot` に置く。編集用checkoutとは分け、
日常実行中に `git pull` / `git commit` / `git push` はしない。

## 秘密情報

`/home/seiya/.config/research-bots/arxiv.env` を作り、`deploy/arxiv.env.example`
の値を設定する。ディレクトリは `0700`、ファイルは `0600` にする。`GITHUB_TOKEN`
には `refrain1539/arxiv-bot` の fine-grained PATを使い、Repository metadata: Read と
Issues: Read and write を与える。Actionsの自動 `GITHUB_TOKEN` はUbuntuからは使えない。

## systemd

- `research-bots-arxiv-daily.timer` — 毎日 09:23, 10:23, 12:23, 14:23, 16:23 JST
- `research-bots-arxiv-reactions.timer` — 10:07〜23:52、および翌00:07〜01:52 JSTの15分間隔

timerは二重送信を避けるため `Persistent=false`。手動実行もservice経由にして、dailyと
reactionsが同じ状態ファイルを同時更新しないよう共有ロックを通す。

```bash
sudo systemctl start research-bots-arxiv-daily.service
sudo systemctl start research-bots-arxiv-reactions.service
sudo systemctl stop research-bots-arxiv-daily.timer research-bots-arxiv-reactions.timer
sudo systemctl status research-bots-arxiv-daily.timer research-bots-arxiv-reactions.timer
journalctl -u research-bots-arxiv-reactions.service -n 200 --no-pager
systemctl list-timers --all 'research-bots-*'
```

更新・復元はnews-botの `docs/ubuntu-operation.md` の手順に従う。timerを停止し、
service終了とバックアップを確認してから行う。`data/` をコード更新で上書きしない。
