# 株シグナル通知bot セットアップガイド（Claude連携版・プログラミング経験ゼロでOK）

毎朝7時（日本時間）にGitHub上のbotが日経225・日経連続増配株・S&P500・米配当貴族をスクリーニングして
結果ファイルを保存 → 毎朝7時半にClaudeのスケジュールタスクがそれを読み取り、
解説コメント付きでClaudeの「予定済み」に投稿する構成です。**Gmail設定は不要。**

作業時間の目安：25〜35分。PC推奨。

---

## STEP 1. GitHubアカウントを作る（5分）

1. https://github.com にアクセスして「Sign up」
2. メールアドレス・パスワードを登録して認証を済ませる

GitHubの「GitHub Actions」という無料機能が、毎朝決まった時間にこのbotを自動実行してくれます。

## STEP 2. リポジトリ（コード置き場）を作る（3分）

1. 画面右上の「＋」→「New repository」
2. Repository name：`stock-signal-bot`（変えてもOKですが、後のSTEP5でURLに使うので覚えておく）
3. **Public を選択**（重要が2つ：①Publicなら自動実行の無料枠が無制限、②Claudeが結果ファイルを
   読みに行けるのはPublicのファイルだけ。リポジトリに秘密情報は一切含まれません）
4. 「Create repository」をクリック

## STEP 3. ファイルをアップロードする（5分）

1. ダウンロードしたzipを解凍する
2. リポジトリ画面の「uploading an existing file」リンクをクリック
   （見当たらなければ「Add file」→「Upload files」）
3. 解凍したフォルダの**中身を全部**ドラッグ＆ドロップ
   - main.py / requirements.txt / SETUP_GUIDE.md / universeフォルダ
4. 「Commit changes」をクリック
5. **`.github`フォルダは隠しフォルダのためドラッグで漏れることがあります。**
   アップロード後にリポジトリに `.github/workflows/daily.yml` が無い場合は、
   「Add file」→「Create new file」でファイル名欄に
   `.github/workflows/daily.yml` と入力（スラッシュで自動的にフォルダになる）し、
   zipの中の daily.yml の中身をコピペして「Commit changes」

## STEP 4. テスト実行する（5分＋待ち時間）

1. リポジトリの「Actions」タブを開く（初回は有効化ボタンを押す）
2. 左の「daily-stock-signal」→右の「Run workflow」→緑のボタンで手動実行
3. 完了まで20〜30分ほど。成功するとリポジトリのトップに `latest_report.md` という
   ファイルが自動生成されます。開いてみて銘柄リストが入っていれば成功🎉
4. 失敗した場合は実行ログの赤い×の箇所をClaudeに貼ってください。原因を特定します。

これ以降は**毎朝7時（日本時間・平日）に自動実行**され、`latest_report.md`が毎日更新されます。

## STEP 5. Claudeのスケジュールタスクを設定する（5分）

1. まず自分のレポートURLを確認します。以下の形式です（`あなたのユーザー名`を置き換え）：
   `https://raw.githubusercontent.com/あなたのユーザー名/stock-signal-bot/main/latest_report.md`
   ブラウザでこのURLを開いて、レポートの文字が表示されればOK。
2. Claudeのサイドバーの「予定済み（Scheduled）」を開き、新しいタスクを作成
   （またはClaudeとのチャットで「毎朝のタスクを登録して」と頼んでもOK）
3. スケジュール：毎日（平日）朝7:30
4. タスクのプロンプトに以下を貼り付け（URLは自分のものに置き換え）：

```
https://raw.githubusercontent.com/あなたのユーザー名/stock-signal-bot/main/latest_report.md
を取得して、株シグナル日報として報告してください。
・カテゴリ（高配当→バリュー→ディフェンシブ→グロース）の順にそのまま掲載
・各銘柄に、事業内容の一言説明と直近の関連ニュースがあれば1行補足を追加
・「注: ○○未確認」の付いた銘柄には、自分で確認すべきポイントを明記
・最後に「本情報は機械的抽出であり投資助言ではない」旨を明記
・レポートの日付が今日でない場合（botの実行失敗の可能性）はその旨を警告
```

※スケジュールタスクは有料プラン（Pro/Max/Team/Enterprise）の機能で、順次展開中のため
　プランや環境によっては未提供の場合があります。使えない場合は下の「代替案」へ。

## 代替案：スケジュールタスクが使えない場合

- 方法A：毎朝Claudeに「今日のシグナル読んで」と一言送る（STEP5のプロンプトを最初に一度
  送っておけば、以降は「今日の分」で通じます）
- 方法B：Discord通知に切り替える。リポジトリのSettings → Secrets and variables → Actions で
  Secretに `DISCORD_WEBHOOK_URL`（DiscordのチャンネルからWebhook URLを発行）、
  Variablesに `NOTIFY_CHANNEL` = `discord` を登録
- 方法C：Gmail通知。Googleアカウントで2段階認証を有効にし
  https://myaccount.google.com/apppasswords で16桁のアプリパスワードを発行。
  Secretに `GMAIL_ADDRESS` と `GMAIL_APP_PASSWORD`、Variablesに `NOTIFY_CHANNEL` = `email` を登録
  （アプリパスワードは絶対に他人に教えない・コードに直接書かないこと）

---

## カスタマイズ

### 基準の調整
`main.py` の冒頭にある `CONFIG = {...}` の数値を書き換えるだけです。
GitHub上でmain.pyを開き、鉛筆マーク（Edit）→編集→「Commit changes」。
例：通知が少なすぎる → `div_yield_min_jp` を 0.030 に下げる、`rsi_high` を 65 に上げる 等。

### 連続増配銘柄リストの更新
`universe/jp_dividend_growers.csv` に日経連続増配株指数の採用銘柄（70銘柄）の
証券コードを追記してください。初期状態では代表的な銘柄のみ入っています。
「日経連続増配株指数 採用銘柄」で検索すると毎月更新の一覧記事が見つかります。

### 実行時間の変更
`.github/workflows/daily.yml` の `cron: "0 22 * * 0-4"` を編集。
UTC表記なので日本時間マイナス9時間です（例：JST 6時 = `0 21 * * 0-4`）。
Claude側のタスク時刻はbot実行完了後（30分以上あと）に設定してください。

---

## 知っておくべき注意点

- **これは投資助言ではありません。** 条件に合致した銘柄を機械的に抽出するだけで、
  値上がりを保証するものでは一切ありません。売買の最終判断は必ず自分で。
- データ源のyfinance（Yahoo Finance）は無料の非公式データのため、
  特に日本株の財務データに欠損や誤りがあり得ます。レポート内の「注: ○○未確認」は
  データ欠損でチェックできなかった項目です。気になる銘柄は必ず一次情報（決算短信・有報）で確認を。
- GitHub Actionsの定時実行は数分〜数十分遅れることがあります（仕様）。
- 市場休場日でも実行されますが、前営業日データでの判定になるだけで実害はありません。
- 精度に不満が出たら、J-Quants API Lightプラン（月1,650円・JPX公式データ）への
  切り替えを検討。その際はClaudeに「J-Quants対応にして」と言えばコードを改修します。
