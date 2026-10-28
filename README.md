# NexusBox — ずんだもん読み上げ Discord BOT

Discord のテキストチャンネルの発言を、**ずんだもんの声**でボイスチャンネルに読み上げる BOT です。
音声合成には [VOICEVOX ENGINE](https://github.com/VOICEVOX/voicevox_engine)（CPU 版）を使い、BOT と ENGINE を Docker Compose でまとめて動かします。
**Ubuntu でも Windows でも動き、起動したあとの操作コマンドは同じです。** GPU は不要です。

> **クレジット表記: VOICEVOX:ずんだもん**
> この BOT を使う際は、VOICEVOX および各キャラクターの利用規約に従ってください。
> <https://voicevox.hiroshiba.jp/> / <https://zunko.jp/con_ongen_kiyaku.html>

---

## 目次

1. [できること](#1-できること)
2. [技術仕様](#2-技術仕様)
3. [事前準備: Discord Bot を作る](#3-事前準備-discord-bot-を作る)
4. [導入: Ubuntu](#4-導入-ubuntu)
5. [導入: Windows](#5-導入-windows)
6. [使い方](#6-使い方)
7. [運用（起動・停止・更新・バックアップ）](#7-運用起動停止更新バックアップ)
8. [`.env` の設定一覧](#8-env-の設定一覧)
9. [トラブルシューティング](#9-トラブルシューティング)
10. [開発者向け](#10-開発者向け)

---

## 1. できること

- ボイスチャンネルに参加して、テキストチャンネルの発言を読み上げる
- 接続中のボイスチャンネルに付いているチャットも読み上げる
- 読み上げるチャンネルを選べる（`/join` したチャンネル／登録したチャンネル／全チャンネル）
- 読み上げ全体の ON/OFF、特定ユーザーのミュート、自分だけ読ませない設定
- 読み方辞書（サーバー専用の置換辞書と、アクセントまで効く ENGINE の辞書の 2 種類）
- URL・コードブロック・メンション・絵文字などを、聞きやすい形に整えてから読む
- ボイスチャンネルの入退室の読み上げ、誰もいなくなったら自動退出
- **操作はすべてスラッシュコマンド**。設定はサーバーごとに保存される

---

## 2. 技術仕様

### 2-1. 構成

```
Discord ◀── Gateway / Voice ──▶ [bot コンテナ]  Python 3.12 + discord.py
                                     │
                                     │ HTTP :50021（Compose の内部ネットワークのみ）
                                     ▼
                                [voicevox コンテナ]  VOICEVOX ENGINE（CPU 版）

どちらのコンテナも、設定・辞書はホスト側の data/ フォルダに保存する
```

| 項目 | 使っているもの |
|---|---|
| 言語 | Python 3.12 |
| Discord | discord.py 2.4 以上（音声機能つき） |
| 音声合成 | VOICEVOX ENGINE（`voicevox/voicevox_engine:cpu-ubuntu24.04-latest`） |
| 音声の再生 | ffmpeg（WAV → Discord 用 PCM）、libopus |
| ENGINE との通信 | aiohttp（REST API） |
| データ保存 | SQLite（aiosqlite、WAL モード） |
| 設定の読み込み | pydantic-settings（`.env`） |
| 実行環境 | Docker Compose（`bot` と `voicevox` の 2 サービス） |

### 2-2. 読み上げの流れ

1 件の発言は、次の順で処理されて音声になります。

```
発言
 │
 ├─ ① 読み上げ対象か判定 ………………… 対象外ならここで終わり（2-3）
 ├─ ② テキスト整形（前半） ……………… メンション・URL などを読める形に（2-4）
 ├─ ③ server 辞書で置換 ………………… 登録した読み方に置き換え（2-5）
 ├─ ④ テキスト整形（後半） ……………… 連打の圧縮・改行・長さ制限（2-4）
 ├─ ⑤ 発言者名・添付ファイルを付ける
 ├─ ⑥ サーバーごとの読み上げキューへ … 1 件ずつ順番に処理
 ├─ ⑦ VOICEVOX で音声合成 ……………… audio_query → 声の設定を反映 → synthesis
 └─ ⑧ ボイスチャンネルで再生
```

- **キュー**: サーバーごとに 1 本。最大 `MAX_QUEUE_SIZE` 件（既定 20）で、満杯のときに来た発言は捨てられます。
- **同時合成数**: 全サーバー合計で ENGINE に同時に送る合成は `SYNTHESIS_CONCURRENCY` 件（既定 2）まで。
- **キャッシュ**: 40 文字以下の文章は、合成した音声を最大 `CACHE_SIZE` 件（既定 256）覚えておき、同じ文章なら合成を省きます。「接続したのだ」や短い定型文が速くなります。
- **頭切れ防止**: 音声の前後に最低 0.1 秒の無音を入れています。

### 2-3. 読み上げる発言・読まない発言

上から順に判定し、どれかに当てはまったら読みません。

| 順 | 読まないもの | 変える方法 |
|---|---|---|
| 1 | DM、BOT 自身の発言、システムメッセージ（ピン留め通知など） | — |
| 2 | BOT がボイスチャンネルに接続していないとき | `/join` する |
| 3 | 読み上げが OFF のとき | `/tts` |
| 4 | 他の BOT の発言 | `/config bot_messages` |
| 5 | `//` または `;;` で始まる発言（メモ・他 BOT のコマンド用） | — |
| 6 | 読み上げ対象ではないチャンネルの発言（下の表） | `/config read_mode`、`/channel add` |
| 7 | ミュートされた人の発言 | `/unmute`、`/ignore-me` |
| 8 | 整形した結果、読むものが何も残らない発言（埋め込みだけの通知など） | — |

**読み上げ対象のチャンネル（`read_mode`）:**

| モード | 読み上げるチャンネル |
|---|---|
| `joined`（既定） | `/join` を実行したチャンネル ＋ `/channel add` で登録したチャンネル |
| `listed` | `/channel add` で登録したチャンネルだけ |
| `all` | BOT から見えるすべてのテキストチャンネル |

- **接続中のボイスチャンネルのチャットは、モードに関係なく常に読みます。**
- スレッドは、親チャンネルが対象なら読みます。

### 2-4. テキスト整形の仕様

**前半（辞書置換の前）**

| 順 | 処理 | 例（入力 → 読み） |
|---|---|---|
| 1 | `xaero-waypoint:` を含む発言は、**発言全体**を差し替えて以降の処理をしない | `xaero-waypoint:拠点:...` → 「うぇいぽいんときょうゆう」 |
| 2 | コードブロック → 「コード」（`skip_codeblock` OFF なら中身を読む） | ` ```print()``` ` → 「コード」 |
| 3 | インラインコード → 中身だけ | `` `foo` `` → 「foo」 |
| 4 | スポイラー → 「ネタバレ」 | `\|\|秘密\|\|` → 「ネタバレ」 |
| 5 | Discord のタイムスタンプ → 「日時」 | `<t:1700000000:R>` → 「日時」 |
| 6 | ユーザーメンション → 表示名 ＋「さん」（不明なら「誰かさん」） | `@ずんだ` → 「ずんださん」 |
| 7 | ロール・チャンネルのメンション → 名前 | `#雑談` → 「雑談」 |
| 8 | カスタム絵文字 → 絵文字名（`read_emoji` OFF なら読まない） | `:zundamon:` → 「zundamon」 |
| 9 | URL → 「ゆーあーるえるしょうりゃく」（`skip_url` OFF ならそのまま） | `https://...` → 「ゆーあーるえるしょうりゃく」 |
| 10 | 引用の `>` と、太字・斜体・取り消し線の記号を消す | `**強調**` → 「強調」 |

**辞書置換**: ここで server 辞書が適用されます（2-5）。整形の後半より先なので、「w」などの読み方も辞書で上書きできます。

**後半（辞書置換の後）**

| 順 | 処理 | 例（入力 → 読み） |
|---|---|---|
| 11 | 2 個以上の `w`（笑い）→ 「わら」。後ろに英字が続くもの（`www.example` など）は対象外 | `それなwww` → 「それな、わら」 |
| 12 | 同じ文字の 4 連続以上 → 3 文字に縮める | `すごーーーーーい` → 「すごーーーい」 |
| 13 | 改行 → 読点、余分な空白と読点を整理 | `いち⏎に` → 「いち、に」 |
| 14 | 最大文字数（`max_length`、既定 120）を超えたら切って「以下略」 | |

**最後に付け足すもの**

```
[発言者名さん]、本文、[てんぷふぁいる]、[スタンプ]
```

- **発言者名**: `/config read_name` が ON のときだけ。表示名にも server 辞書を通し、20 文字までに切ります。
- **てんぷふぁいる**: 添付ファイルがあるとき。**何枚あっても 1 回だけ**読みます。
- **スタンプ**: スタンプがあるとき。
- 添付ファイルとスタンプは `/config read_attachment` が ON のときだけ付きます。

実際の読み方は、Discord で **`/dict test text:<文章>`** を打つと確認できます。

### 2-5. 読み方辞書（2 層構成）

| | `server`（既定） | `global` |
|---|---|---|
| 保存先 | BOT の SQLite | VOICEVOX ENGINE のユーザー辞書（SQLite にも控えを保存） |
| 効く範囲 | **そのサーバーだけ** | **この BOT がいる全サーバー** |
| 仕組み | 合成の前に文章を置換する | ENGINE が文章を解析する段階で読みを決める |
| アクセント | 指定できない | 指定できる（省略すると自動推定） |
| 必要な権限 | メッセージの管理 | サーバー管理 |
| 向いている用途 | メンバー名、サーバー内の言葉、略語 | 難しい固有名詞、一般的な言葉の読み間違い |

- 読みは**ひらがなでもカタカナでも入力できます**（保存時にカタカナにそろえます）。
- server 辞書は**長い表記から優先**して置き換えます（「東京都」と「東京」を登録すると「東京都」が先）。英字の大文字小文字は区別しません。
- global 辞書が ENGINE から消えても（`data/voicevox/` を消した、など）、**BOT の起動時に SQLite の控えから自動で再登録**されます。

### 2-6. 声

**声はずんだもん（ノーマル）で固定**で、全員・全サーバー共通です。Discord から変えるコマンドはありません。
話す速さなどは `.env` で調整し、反映には BOT の再起動が必要です。

| 変数 | 範囲 | 既定 | 意味 |
|---|---|---|---|
| `DEFAULT_SPEAKER_ID` | — | 3 | 話者 ID（3 = ずんだもん ノーマル） |
| `DEFAULT_SPEED` | 0.5 〜 2.0 | 1.0 | 話す速さ |
| `DEFAULT_PITCH` | -0.15 〜 0.15 | 0.0 | 声の高さ |
| `DEFAULT_INTONATION` | 0.0 〜 2.0 | 1.0 | 抑揚（0 で棒読み） |
| `DEFAULT_VOLUME` | 0.0 〜 2.0 | 1.0 | 音量 |

範囲外の値は、起動時に範囲内へ丸められます。

### 2-7. 入退室と自動退出

- `/join` すると「接続したのだ」と喋ります。
- BOT がいるボイスチャンネルに人が出入りすると「〇〇さんが入室したのだ」「〇〇さんが退室したのだ」と読みます（`/config read_join_leave`）。
- **人間が全員いなくなったら自動で退出**し、`/join` したチャンネルにお知らせを送ります（`/config auto_leave`）。他の BOT が残っていても退出します。
- **BOT を再起動しても、ボイスチャンネルには自動で戻りません。** もう一度 `/join` してください。設定や辞書はそのまま残っています。

### 2-8. データの保存場所

```
data/
├─ nexusbox.db   サーバーごとの設定・読み上げ対象チャンネル・ミュート・辞書（server / global とも）
└─ voicevox/     VOICEVOX ENGINE のユーザー辞書（global 辞書の本体）
```

- コンテナを削除しても `data/` は消えません。
- **`data/nexusbox.db` さえあれば、global 辞書も含めてすべて復旧できます。**
- 設定はメモリにもキャッシュしていて、変更したときにそのサーバーの分だけ読み直します。

### 2-9. 権限

**BOT に必要な Discord の設定**

- 特権インテント: `MESSAGE CONTENT INTENT`（発言内容を読む）、`SERVER MEMBERS INTENT`（表示名を調べる）
- BOT の権限: `View Channels` / `Send Messages` / `Embed Links` / `Attach Files` / `Read Message History` / `Connect` / `Speak`

**コマンドを使える人**

| 区分 | コマンド |
|---|---|
| 誰でも | `/join` `/disconnect` `/skip` `/clear` `/status` `/help` `/ignore-me`、`/dict list` `search` `test` `export` |
| メッセージの管理 | `/dict add` `remove` `import`（server 辞書） |
| サーバー管理 | `/tts` `/mute` `/unmute`、`/channel ...`、`/config ...`、global 辞書の編集 |

「サーバー管理」が必要なコマンドは、Discord の「サーバー設定 → 連携サービス」でロールごとに使える人を変えられます。

---

## 3. 事前準備: Discord Bot を作る

Ubuntu・Windows 共通です。

### 3-1. アプリケーションを作る

1. <https://discord.com/developers/applications> を開き、**New Application** でアプリを作る
2. 左のメニューから **Bot** を開く

### 3-2. 特権インテントを有効にする（必須）

**Bot** ページの *Privileged Gateway Intents* で、次の 2 つを **ON** にして保存します。

- ✅ `MESSAGE CONTENT INTENT`
- ✅ `SERVER MEMBERS INTENT`

> 忘れると、BOT が起動時に `PrivilegedIntentsRequired` というエラーで止まります。

### 3-3. トークンを控える

**Bot → Reset Token** を押すと表示される文字列が**トークン**です。あとで `.env` に書きます。

- トークンは**パスワードと同じ**です。他人に見せたり、GitHub に上げたりしないでください。
- 漏れたときや分からなくなったときは、もう一度 **Reset Token** を押せば新しいものに変わります（古いトークンは使えなくなります）。

### 3-4. サーバーに招待する

**OAuth2 → URL Generator** で次を選び、下に出る URL を開いて自分のサーバーに追加します。

- **Scopes**: `bot`, `applications.commands`
- **Bot Permissions**: `View Channels` / `Send Messages` / `Embed Links` / `Attach Files` / `Read Message History` / `Connect` / `Speak`

URL を手で作る場合は、`<APP_ID>` を **General Information** の Application ID に置き換えてください。

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=2150747136
```

---

## 4. 導入: Ubuntu

Ubuntu Server 24.04 を想定しています。他の Linux でも Docker が動けば同じ手順です。

**必要なもの**: メモリ 4GB 以上、空きディスク 5GB 程度（ENGINE のイメージが約 1.5GB）

### 4-1. Docker を入れる

```bash
# Docker Engine と Compose を入れる
curl -fsSL https://get.docker.com | sudo sh

# sudo なしで docker を使えるようにする（反映には一度ログアウトして入り直す）
sudo usermod -aG docker $USER

# Docker をサーバー起動時に自動で起動させる
sudo systemctl enable --now docker
```

入り直したら、次が表示されれば準備完了です。

```bash
docker compose version
```

### 4-2. NexusBox を置く

```bash
cd ~
git clone https://github.com/Tamago0314/NexusBox.git nexusbox
cd nexusbox
```

git を使わない場合は、GitHub の **Code → Download ZIP** で落として `~/nexusbox` に展開しても構いません。

### 4-3. `.env` を作る

```bash
cp .env.example .env
nano .env
```

`DISCORD_TOKEN=` の後ろに、[3-3](#3-3-トークンを控える) で控えたトークンを貼ります。**他の項目はそのままで動きます。**

```
DISCORD_TOKEN=ここにトークン
```

- `"` で囲まない、`=` の前後に空白を入れない
- nano の保存は `Ctrl+O` → `Enter`、終了は `Ctrl+X`

### 4-4. 辞書フォルダを用意する（初回だけ）

VOICEVOX ENGINE はコンテナの中で uid 1000 のユーザーとして動きます。
フォルダを Docker に自動で作らせると root の持ち物になって global 辞書を保存できないため、先に作っておきます。

```bash
mkdir -p data/voicevox
sudo chown -R 1000:1000 data/voicevox
```

### 4-5. 起動する

```bash
docker compose up -d --build
```

**初回は ENGINE のダウンロード（約 1.5GB）と準備で数分かかります。** BOT は ENGINE の準備ができるまで最大 10 分、自動で待ちます。

### 4-6. 起動できたか確認する

```bash
docker compose ps
```

`bot` と `voicevox` が両方 `Up`（`voicevox` は `Up (healthy)`）になっていれば正常です。
`Up (health: starting)` の間は ENGINE の準備中なので、1〜2 分待ってください。

BOT のログも見ておきます（`Ctrl+C` で抜けます。BOT は止まりません）。

```bash
docker compose logs -f bot
```

`logged in as <BOT の名前>` と `グローバルに N 個のコマンドを同期しました` が出れば成功です。
続けて [6. 使い方](#6-使い方) へ進んでください。

### 4-7. サーバーを再起動したとき

コンテナには `restart: unless-stopped` を付けてあるので、**サーバーを再起動すると自動で立ち上がります。**
ただし、`docker compose stop` や `down` で**自分で止めていた場合は上がってきません**。そのときは起動してください。

```bash
cd ~/nexusbox
docker compose up -d
```

止めていたかどうかに関係なく毎回起動したい場合は、systemd に登録します。
`WorkingDirectory` は自分のパスに直してください（`~` は使えないので `/home/ユーザー名/nexusbox` のように書きます）。

```bash
sudo tee /etc/systemd/system/nexusbox.service > /dev/null <<'EOF'
[Unit]
Description=NexusBox (Zundamon TTS Discord Bot)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ユーザー名/nexusbox
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nexusbox.service
```

登録したあとは、`sudo systemctl restart nexusbox` / `sudo systemctl stop nexusbox` で操作すると混乱しません。

---

## 5. 導入: Windows

Windows 10（22H2 以降）/ Windows 11 を想定しています。Docker Desktop を使うので、**起動したあとのコマンドは Ubuntu と同じ**です。

**必要なもの**: メモリ 8GB 以上を推奨（Docker Desktop 自体もメモリを使います）、空きディスク 10GB 程度

### 5-1. WSL2 と Docker Desktop を入れる

1. **スタートボタンを右クリック → 「ターミナル（管理者）」**（または「Windows PowerShell（管理者）」）を開き、次を実行して PC を再起動します。

   ```powershell
   wsl --install
   ```

   すでに入っている場合は「既にインストールされています」と出るので、そのまま次へ進みます。

2. <https://www.docker.com/products/docker-desktop/> から **Docker Desktop for Windows** をダウンロードしてインストールします。
   途中で聞かれたら **「Use WSL 2 instead of Hyper-V」** にチェックを入れたままにします。

3. Docker Desktop を起動し、画面左下が **Engine running**（緑）になるまで待ちます。

4. 普通の PowerShell を開き、次が表示されれば準備完了です。

   ```powershell
   docker compose version
   ```

> Docker Desktop は個人利用・小規模な組織なら無料です。詳しくは Docker の利用規約を確認してください。

### 5-2. NexusBox を置く

**git を使う場合**（git が無ければ `winget install Git.Git` で入ります）:

```powershell
cd C:\
git clone https://github.com/Tamago0314/NexusBox.git nexusbox
cd C:\nexusbox
```

**git を使わない場合**: GitHub の **Code → Download ZIP** で落とし、`C:\nexusbox` に展開します（フォルダを開いたときに `compose.yaml` が見える状態にします）。

> **OneDrive の中（デスクトップ・ドキュメントなど）には置かないでください。** 同期がデータベースのファイルを掴んで、壊れる原因になります。`C:\nexusbox` のような場所がおすすめです。

### 5-3. `.env` を作る

```powershell
cd C:\nexusbox
Copy-Item .env.example .env
notepad .env
```

メモ帳が開くので、`DISCORD_TOKEN=` の後ろに [3-3](#3-3-トークンを控える) で控えたトークンを貼って保存します。**他の項目はそのままで動きます。**

```
DISCORD_TOKEN=ここにトークン
```

- `"` で囲まない、`=` の前後に空白を入れない
- エクスプローラーでは `.env` が「ENV ファイル」と表示されます。`.env.txt` になっていたら名前を `.env` に直してください

### 5-4. 辞書フォルダを用意する（初回だけ）

```powershell
New-Item -ItemType Directory -Force data\voicevox
```

Windows では Ubuntu のような所有者の変更（`chown`）は要りません。

### 5-5. 起動する

```powershell
docker compose up -d --build
```

**初回は ENGINE のダウンロード（約 1.5GB）と準備で数分かかります。** BOT は ENGINE の準備ができるまで最大 10 分、自動で待ちます。

### 5-6. 起動できたか確認する

```powershell
docker compose ps
docker compose logs -f bot
```

見方は Ubuntu と同じです（[4-6](#4-6-起動できたか確認する)）。Docker Desktop の **Containers** 画面でも `nexusbox` の中の 2 つが緑になっているのが見えます。
続けて [6. 使い方](#6-使い方) へ進んでください。

### 5-7. PC を再起動したとき

**Docker Desktop が起動していないと BOT も動きません。** PC の起動と一緒に動かすには、次の設定をしておきます。

- Docker Desktop の **Settings（歯車）→ General → 「Start Docker Desktop when you sign in to your computer」** を ON

これで、Windows にサインインすると Docker Desktop が起動し、BOT も自動で立ち上がります（`docker compose stop` で止めていた場合を除く）。

- **サインインするまでは起動しません。** 常に動かしたい場合は、自動サインインを設定するか、Ubuntu サーバーでの運用を検討してください。
- **PC がスリープすると BOT も止まります。** 「設定 → システム → 電源」でスリープしないようにしておくと安心です。

止めていた場合は、自分で起動します。

```powershell
cd C:\nexusbox
docker compose up -d
```

---

## 6. 使い方

ここからは Discord 上の操作なので、Ubuntu・Windows 共通です。

### 6-1. 最初の動作確認

この順番で試すと、どこで詰まっているか分かりやすいです。

1. **BOT がオンラインか** — メンバー一覧で BOT がオンラインになっている
2. **`/help`** — コマンド一覧が出れば、コマンドの登録は成功
   - 候補に出てこない場合は [9. トラブルシューティング](#9-トラブルシューティング) へ（反映に最大 1 時間かかることがあります）
3. **`/status`** — 「VOICEVOX ENGINE」の欄にバージョンが出れば、ENGINE との通信も成功
4. **ボイスチャンネルに入って `/join`** — 「接続したのだ」と喋れば完成
5. **そのチャンネルで何か書く** — 読み上げられる
6. **`/disconnect`** — 退出する

### 6-2. 普段の使い方

1. ボイスチャンネルに入る
2. 読み上げてほしいテキストチャンネルで **`/join`**
3. 普通にチャットする（BOT が読み上げる）
4. 終わったら **`/disconnect`**（全員が抜ければ自動でも退出します）

**サーバーや PC を再起動したあとは、もう一度 `/join` するだけ**で、前回の設定のまま再開します。

### 6-3. コマンド一覧

**接続**

| コマンド | 説明 |
|---|---|
| `/join [channel]` | ボイスチャンネルに参加。省略すると自分がいる VC。**実行したテキストチャンネルが読み上げ対象になる** |
| `/disconnect` | 退出（読み上げ待ちも消える） |
| `/skip` | いま読んでいる 1 件を飛ばす |
| `/clear` | 読み上げ待ちを全部消す（長文を連投されたときなど） |
| `/status` | 接続先・読み上げ待ちの件数・声・ENGINE のバージョン・キャッシュの状況 |
| `/help` | コマンド一覧（自分にだけ見える） |

**読み上げるチャンネル**（サーバー管理）

| コマンド | 説明 |
|---|---|
| `/channel add [channel]` | 読み上げ対象に追加（省略すると実行したチャンネル） |
| `/channel remove [channel]` | 対象から外す |
| `/channel list` | 対象の一覧 |
| `/channel clear` | 登録を全部解除 |

**読み上げの切り替え**

| コマンド | 権限 | 説明 |
|---|---|---|
| `/tts [enabled]` | サーバー管理 | 読み上げ全体の ON/OFF。省略すると今の状態を反転。VC からは抜けない |
| `/mute <user>` | サーバー管理 | その人の発言を読まない |
| `/unmute <user>` | サーバー管理 | ミュートを解除 |
| `/ignore-me` | 誰でも | **自分の発言だけ**読ませない。もう一度打つと元に戻る |

**挙動の設定**（サーバー管理）

| コマンド | 既定 | 説明 |
|---|---|---|
| `/config read_mode <mode>` | joined | 読み上げるチャンネルの決め方（[2-3](#2-3-読み上げる発言読まない発言)） |
| `/config read_name <bool>` | OFF | 「〇〇さん」と発言者名を読む |
| `/config bot_messages <bool>` | OFF | 他の BOT の発言も読む |
| `/config read_join_leave <bool>` | ON | VC の入退室を読む |
| `/config auto_leave <bool>` | ON | VC から人がいなくなったら自動退出 |
| `/config skip_url <bool>` | ON | URL を「ゆーあーるえるしょうりゃく」と省略 |
| `/config skip_codeblock <bool>` | ON | コードブロックを「コード」と省略 |
| `/config read_emoji <bool>` | ON | カスタム絵文字を名前で読む |
| `/config read_attachment <bool>` | ON | 添付ファイル・スタンプを読む |
| `/config max_length <1〜500>` | 120 | 1 発言で読む最大の文字数 |
| `/config show` | — | 今の設定を一覧表示 |
| `/config reset` | — | 設定を初期状態に戻す（辞書と登録チャンネルは残る） |

**読み方辞書**

| コマンド | 権限 | 説明 |
|---|---|---|
| `/dict add <表記> <読み> [accent] [scope]` | server: メッセージの管理 / global: サーバー管理 | 読み方を登録（同じ表記なら上書き） |
| `/dict remove <表記> [scope]` | 同上 | 解除（表記は入力候補が出る） |
| `/dict list [scope]` | 誰でも | 一覧（15 件ごとにページ送り） |
| `/dict search <keyword> [scope]` | 誰でも | 表記・読みで部分一致検索 |
| `/dict test <text> [speak]` | 誰でも | どう読まれるかを確認。`speak: True` で実際に喋らせる |
| `/dict export [scope]` | 誰でも | JSON ファイルで書き出す |
| `/dict import <file> [scope]` | server: メッセージの管理 / global: サーバー管理 | JSON から一括登録（1MB・500 件まで） |

`scope` を省略すると `server` 辞書になります。

### 6-4. よく使う例

```
# 「草」を「くさ」と読ませる（このサーバーだけ）
/dict add 表記:草 読み:くさ

# どう読まれるか確かめる
/dict test text:それは草 https://example.com

# 決めたチャンネルだけ読ませる
/config read_mode mode:listed
/channel add channel:#読み上げ

# 少しの間だけ黙らせる（もう一度打つと再開）
/tts
```

---

## 7. 運用（起動・停止・更新・バックアップ）

コマンドは Ubuntu・Windows 共通です。**必ずプロジェクトのフォルダ（`compose.yaml` がある場所）で実行**してください。

- Ubuntu: `cd ~/nexusbox`
- Windows: PowerShell で `cd C:\nexusbox`

### 7-1. よく使うコマンド

| やりたいこと | コマンド |
|---|---|
| 起動 | `docker compose up -d` |
| 状態を見る | `docker compose ps` |
| BOT のログを見る | `docker compose logs -f bot`（`Ctrl+C` で抜ける） |
| ENGINE のログを見る | `docker compose logs -f voicevox` |
| 一時停止（設定・辞書は残る） | `docker compose stop` |
| コンテナを消して止める（`data/` は残る） | `docker compose down` |
| 再起動 | `docker compose restart` |
| `.env` を変えたあとの反映 | `docker compose up -d --force-recreate bot` |

> `.env` の変更は `restart` では反映されません。`--force-recreate` を使ってください。

### 7-2. アップデート

```bash
git pull                       # コードを最新にする（ZIP の場合は上書き展開）
docker compose pull voicevox   # ENGINE も最新にする場合
docker compose up -d --build   # 作り直して起動
```

`data/` はそのまま引き継がれるので、設定・辞書は失われません。

### 7-3. バックアップと復元

**バックアップ**（BOT を止めてから取ると確実です）

Ubuntu:

```bash
docker compose stop
tar czf nexusbox-backup-$(date +%Y%m%d).tar.gz data/
docker compose up -d
```

Windows（PowerShell）:

```powershell
docker compose stop
Compress-Archive -Path data -DestinationPath "nexusbox-backup-$(Get-Date -Format yyyyMMdd).zip"
docker compose up -d
```

**復元**: BOT を止め、`data/` をバックアップの中身で置き換えてから起動します。
`data/nexusbox.db` だけでも、global 辞書を含めてすべて戻ります。

- `.env` にはトークンが入っています。バックアップに含める場合は扱いに気をつけてください。
- 辞書だけなら、Discord から `/dict export` → `/dict import` で別のサーバーにも移せます。

---

## 8. `.env` の設定一覧

最低限必要なのは `DISCORD_TOKEN` だけです。変更したら `docker compose up -d --force-recreate bot` で反映します。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `DISCORD_TOKEN` | （なし） | **必須。** Discord Bot のトークン |
| `VOICEVOX_URL` | `http://voicevox:50021` | ENGINE の場所。Docker Compose で動かすなら変更不要 |
| `SYNTHESIS_CONCURRENCY` | `2` | ENGINE に同時に送る合成の数。CPU では増やしすぎるとかえって遅くなる |
| `DEFAULT_SPEAKER_ID` | `3` | 話者 ID（3 = ずんだもん ノーマル） |
| `DEFAULT_SPEED` | `1.0` | 話す速さ（0.5 〜 2.0） |
| `DEFAULT_PITCH` | `0.0` | 声の高さ（-0.15 〜 0.15） |
| `DEFAULT_INTONATION` | `1.0` | 抑揚（0.0 〜 2.0） |
| `DEFAULT_VOLUME` | `1.0` | 音量（0.0 〜 2.0） |
| `CACHE_SIZE` | `256` | 合成した音声を覚えておく件数（0 で無効） |
| `MAX_QUEUE_SIZE` | `20` | 1 サーバーあたりの読み上げ待ちの上限 |
| `DATABASE_PATH` | `data/nexusbox.db` | 設定と辞書を保存するファイル |
| `LOG_LEVEL` | `INFO` | `DEBUG` にすると詳しいログが出る |
| `GUILD_IDS` | （空） | 開発用。サーバー ID をカンマ区切りで書くと、そのサーバーにだけコマンドが**すぐ**反映される |

**`GUILD_IDS` について**: 空のときはコマンドを Discord 全体に登録するので、**反映に最大 1 時間**かかります。
すぐ試したいときはサーバー ID を書いてください（Discord の「設定 → 詳細設定 → 開発者モード」を ON にして、サーバー名を右クリック →「サーバー ID をコピー」）。

```
GUILD_IDS=123456789012345678
GUILD_IDS=123456789012345678,987654321098765432
```

---

## 9. トラブルシューティング

まず `docker compose ps` と `docker compose logs --tail 50 bot` で、BOT が動いているか・どんなエラーが出ているかを確認してください。

### BOT が `Restarting` を繰り返す

ログの最後のエラーで原因が分かります。

| ログに出るもの | 原因と直し方 |
|---|---|
| `LoginFailure` / `Improper token` / `401 Unauthorized` | トークンが間違っている、または古い。[3-3](#3-3-トークンを控える) で再発行して `.env` に貼り直し、`docker compose up -d --force-recreate bot` |
| `PrivilegedIntentsRequired` | 特権インテントが OFF。[3-2](#3-2-特権インテントを有効にする必須) を ON にして再起動 |
| `VOICEVOX ENGINE (...) に接続できませんでした` | ENGINE が起動していない、または `VOICEVOX_URL` が違う。`docker compose ps` で `voicevox` を確認し、`.env` の `VOICEVOX_URL` を `http://voicevox:50021` にする |

### スラッシュコマンドが出てこない

- 全体への登録は**反映に最大 1 時間**かかります。急ぐときは `.env` の `GUILD_IDS` にサーバー ID を入れて反映し直してください
- 招待するときに `applications.commands` を付け忘れていると出ません。[3-4](#3-4-サーバーに招待する) の URL で招待し直してください
- Discord アプリの再読み込み（`Ctrl+R`）で出ることもあります

### `/join` しても読まれない

順に確認してください。

1. `/status` の「読み上げ」が **ON** か（`/tts enabled:True` で戻る）
2. `/status` の「対象チャンネル」が、書き込んでいるチャンネルと合っているか
3. 自分で `/ignore-me` していないか（もう一度打てば戻る）
4. `/dict test` で、読み上げる文章が空になっていないか
5. BOT がそのチャンネルを見る権限（`View Channels`）を持っているか

### 音が途切れる・遅れる

- `SYNTHESIS_CONCURRENCY` を `2` 以下にする（CPU では増やすとかえって遅くなります）
- `docker compose logs voicevox` に ENGINE のエラーが出ていないか確認する
- 長文が大量に溜まったときは `/clear` で読み上げ待ちを消す

### global 辞書に登録できない・消える（Ubuntu）

`data/voicevox/` が root の持ち物になっていると、ENGINE が辞書を保存できません。

```bash
sudo chown -R 1000:1000 data/voicevox
docker compose restart voicevox
```

ENGINE 側の辞書が消えても、BOT の起動時に自動で再登録されます。

### コンテナが上がってこない

- **Ubuntu**: `systemctl is-enabled docker` が `enabled` か確認する。`stop` / `down` していたなら `docker compose up -d`
- **Windows**: Docker Desktop が起動しているか確認する（[5-7](#5-7-pc-を再起動したとき)）

### Windows 特有のエラー

| 症状 | 原因と直し方 |
|---|---|
| `error during connect` / `open //./pipe/docker_engine` | Docker Desktop が起動していない。起動して **Engine running** になるまで待つ |
| Docker Desktop が「WSL 2 が必要」と言う | 管理者の PowerShell で `wsl --install` → PC を再起動 |
| 「仮想化が無効」と出る | BIOS / UEFI の設定で仮想化（Intel VT-x / AMD-V / SVM）を有効にする |
| `no configuration file provided: not found` | `compose.yaml` のあるフォルダにいない。`cd C:\nexusbox` してから実行する（Ubuntu でも同じ） |

---

## 10. 開発者向け

### 10-1. ローカルでテストする

Python 3.11 以上が必要です。テストは Discord や ENGINE に接続せずに動きます。

Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Windows（PowerShell）:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest
```

`Activate.ps1` が「スクリプトの実行が無効」で止まる場合は、一度だけ `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を実行してください。

BOT 自体をコンテナの外で動かす場合は、別に VOICEVOX ENGINE を起動して、`.env` の `VOICEVOX_URL` を `http://localhost:50021` に変えてから `python -m nexusbox` を実行します。

### 10-2. ディレクトリ構成

```
nexusbox/
├─ compose.yaml          bot と voicevox（CPU 版 ENGINE）の 2 サービス
├─ Dockerfile            bot のイメージ（python:3.12-slim + ffmpeg + libopus）
├─ .env.example          .env のひな形
├─ requirements.txt      実行に必要なライブラリ
├─ requirements-dev.txt  テスト用のライブラリ
├─ data/                 設定と辞書の保存先（Git には含めない）
├─ tests/                ユニットテスト
└─ nexusbox/
   ├─ __main__.py        起動処理（python -m nexusbox）
   ├─ bot.py             BOT 本体。各部品の生成と Cog の読み込み
   ├─ config.py          .env の読み込み
   ├─ models.py          サーバー設定・辞書エントリ・声のパラメータの型
   ├─ db.py              SQLite への保存と読み出し
   ├─ voicevox.py        VOICEVOX ENGINE の API クライアント
   ├─ textproc.py        テキスト整形（2-4 の処理）
   ├─ dictionary.py      読み方辞書（2-5 の処理）
   ├─ player.py          読み上げキュー・合成キャッシュ・再生
   └─ cogs/
      ├─ reader.py       発言の読み上げ、入退室、自動退出
      ├─ connection.py   /join /disconnect /skip /clear /status /help
      ├─ channels.py     /channel
      ├─ settings.py     /tts /mute /unmute /ignore-me /config
      └─ dictionary.py   /dict
```

---

## ライセンスと注意

- **クレジット表記「VOICEVOX:ずんだもん」は必須**です。`/help` と `/status` の下に表示しています
- VOICEVOX の音声を配信や動画で使う場合は、各キャラクターの利用規約を確認してください
- `.env` に入る Discord トークンは絶対に公開しないでください（`.gitignore` で Git に含めないようにしてあります）
