# 🤖 Discord AI Bot

ローカル LLM (Ollama / llama-server) をバックエンドに備えた、高機能かつ自律的な Discord AI チャット Bot です。  
感情シミュレーション、長期的エピソード記憶、ユーザー関係値モデル、Chrome MCP による Web 調査、話題乗っ取り防止（Anti-Hijack）ツリー保護などを統合しています。

---

## ✨ 主な機能

- 🧠 **ローカル LLM 推論連携 (Ollama / llama-server)**:
  - Qwen3.6 などの推論（Reasoning）モデルにネイティブ対応。
  - 会話生成、中間タスク（思考選別、感情分析）、画像認識（Vision）のマルチモデル運用。
  - Speculative Decoding (llama-server) による超高速推論に対応。
- 💖 **動的感情状態 & ユーザー関係値システム**:
  - 喜び・怒り・悲しみなどの感情パラメータが会話によって動的に変動。
  - ユーザーごとの親愛度・信頼度（関係値）と所感を自動学習・蓄積。
  - アトミック書き込みによる安全な永続化と、時間経過による感情減衰（Neutral回帰）。
- 📚 **ハイブリッド長期記憶システム (MemoryStore)**:
  - SQLite (FTS5 全文検索) + `sqlite-vec` (ベクトル検索) による高精度な記憶想起。
  - 記憶DB検索 → Web自然調査 → 不知表明の3段階フォールバック。
- 🛡️ **会話ツリー分離 & Anti-Hijack 機構**:
  - Discord 返信（Reply）時に親メッセージを最優先文脈として保護。
  - 並行する別ユーザーの雑談から目的語や話題が混入する「話題乗っ取り」を根本遮断。
  - 自然な相槌を保護しつつ、オウム返しを検知・抑止する多層ガード。
- 🌐 **Web 自然調査 (Chrome MCP Server)**:
  - 会話中の疑問や評判の調査が必要な際、ヘッドレス/常駐 Chrome 経由で自然に調査を行い、会話の中で 1〜2 文のネット評判として回答。
- ⏰ **自発発話（Proactive Behavior）**:
  - 会話が途切れた放置時間や退屈度（boredom）に応じて、過去の記憶やユーザー関心事を元に Bot が自発的に話題を振る機能。

---

## 📁 ディレクトリ構成

```text
discord-ai-bot/
├── lib/                     # 共通ユーティリティ（JSON, 日付, 設定, テキスト, Discord）
├── ollama_bot/              # Bot 本体パッケージ
│   ├── config_base.py       # 基本設定ベース
│   ├── config_loader.py     # 設定モジュール動的ローダー
│   ├── config_sample.py     # キャラクター設定サンプル・テンプレート
│   ├── common/              # メモリ、Web調査、プロンプト生成などの共通ロジック
│   ├── ollama_chat_/        # チャット Cog（感情、記憶、イベント、返答パイプライン）
│   └── main.py              # エントリポイント
├── modelfiles/              # Ollama Modelfile 定義
├── restart-ai.sh            # サービス管理・起動スクリプト
├── control_single_ollama.py # 単一プロセス制御スクリプト
├── requirements.txt         # 依存関係定義
└── .env.example             # 環境変数テンプレート
```

---

## 🚀 クイックスタート

### 1. 必要要件
- **Python**: 3.11 以上 (3.11 〜 3.14 対応)
- **Ollama** または **llama-server**: ローカル推論環境
- **Discord Bot Token**: Discord Developer Portal から取得
- **Google Chrome / Chromium & Node.js** (任意): Chrome MCP による Web 自然調査機能を利用する場合

### 2. インストール
```bash
# リポジトリのクローン
git clone https://github.com/your-username/discord-ai-bot.git
cd discord-ai-bot

# 仮想環境の作成と有効化
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存パッケージのインストール
pip install -r requirements.txt
```

### 3. Discord Developer Portal の設定 (OAuth2 / Intents)
Bot の登録およびサーバー招待時に、以下の権限とインテントを設定します。

#### ① Privileged Gateway Intents の有効化（必須）
Developer Portal の **[Bot]** タブ ＞ **Privileged Gateway Intents** で以下を **ON** にして **Save Changes** で保存します。未設定の場合、起動時にエラーとなります。
- **Server Members Intent**: メンバー情報取得・ニックネーム変更検知
- **Message Content Intent**: メッセージ本文の読み取り（AI応答に必須）

#### ② サーバー招待リンクの生成 (OAuth2)
**[OAuth2] ＞ [URL Generator]** で招待 URL を生成してサーバーに追加します。
- **SCOPES**:
  - `bot`（現在の Discord 仕様ではスラッシュコマンド権限も自動付加されます）
- **BOT PERMISSIONS**:
  - 一般: `Change Nickname`（感情に応じたニックネーム・表情の自動変更）
  - テキスト: `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`, `Embed Links`, `Add Reactions`（任意: `Attach Files`, `Use External Emojis`）
  *(※テスト環境等では `Administrator` による一括許可も可能)*

### 4. 環境設定 (.env)
`.env.example` をコピーして `.env` を作成し、必要なトークンや設定値を記入します。

```bash
cp .env.example .env
```

主な設定項目:
```ini
# Discord Bot トークン
DISCORD_TOKEN=your_bot_token_here

# 稼働させる Discord サーバー (Guild) ID
DISCORD_GUILD_ID=123456789012345678

# メイン応答チャンネル ID
OLLAMA_CHANNEL_ID=123456789012345678

# Ollama API エンドポイント
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

### 5. キャラクター設定ファイルの作成
`config_sample.py` をコピーして、あなた専用のキャラクター設定ファイル（例: `config_mybot.py`）を作成します。

```bash
cp ollama_bot/config_sample.py ollama_bot/config_mybot.py
```
`config_mybot.py` を開き、Botの名前、一人称、口調、コアバリュー等を自由にカスタマイズしてください。

### 6. Modelfile の作成 (Ollama)
`modelfiles/` ディレクトリに用意されている Modelfile からモデルを作成します。

```bash
ollama create middle-llm -f modelfiles/middle-llm.Modelfile
ollama create classifier-local-v2 -f modelfiles/classifier-v2.Modelfile
```

### 7. Bot の起動

#### スクリプトから起動する場合
```bash
./restart-ai.sh
```

#### 直接 Python で起動する場合
```bash
export OLLAMA_BOT_CONFIG=config_mybot
python3 -m ollama_bot.main
```

---

## 🌐 Chrome MCP サーバーの連携設定 (Web 自然調査)

本 Bot は **Chrome MCP (Model Context Protocol) サーバー** と連携することで、会話中の疑問や最新のネット評判、X (Twitter) タイムラインなどをブラウザ経由で自動調査できます。

### 📋 動作条件

1. **MCP HTTP エンドポイントの疎通**:
   - デフォルト: `http://127.0.0.1:12306/mcp`
   - Chrome 拡張機能およびネイティブホストが起動し、Streamable HTTP (MCP プロトコルバージョン `2025-03-26`) を受け付けていること。
2. **必須 MCP ツールの提供**:
   - Web 閲覧・解析に必要な以下のツールが MCP サーバーから正常に提供されていること:
     - `chrome_navigate`: 指定 URL への移動
     - `chrome_get_web_content`: ページ本文・DOM コンテンツの抽出
     - （任意: `chrome_read_page`, `chrome_close_tabs`, `chrome_get_page_metadata`, `chrome_screenshot` 等）
   - ※ 安全性のため、入力・送信・購入操作（`click`, `type`, `submit`, `buy` 等）のツールは自動的にブロック・除外されます。
3. **ディスプレイ環境 (Linux サーバーの場合)**:
   - Chrome を起動するための X11 / Wayland / Xvfb 環境（`DISPLAY=:0` など）が利用可能であること。
4. **セッション永続性と単一接続**:
   - MCP サーバーはトランスポート接続を維持するため、セッションファイル（`/tmp/discord-ai-bot-chrome-mcp-session`）を保持して再利用します。多重接続エラーが発生した場合は Chrome プロファイルの再起動が必要です。

### 🛠️ 準備すべきこと (セットアップ手順)

#### ① Google Chrome または Chromium のインストール
- **Linux (Ubuntu/Debian)**: `sudo apt install -y google-chrome-stable` または `chromium-browser`
- **Linux (Arch)**: `sudo pacman -S google-chrome` または `chromium`
- **macOS**: `Google Chrome.app` をインストール

#### ② `mcp-chrome-bridge` のインストールとマニフェスト登録
Chrome 拡張機能とネイティブホストを中継するブリッジツール（Node.js / npm）をインストールし、Native Messaging マニフェストを登録します。
```bash
# グローバルインストール
npm install -g mcp-chrome-bridge

# ネイティブホストマニフェストの登録
mcp-chrome-bridge register
```

#### ③ 専用 OS ユーザーと Chrome プロファイルの準備 (Linux 推奨)
Linux サーバーで root 運用する場合、Chrome は root 実行を制限するため、専用ユーザー（例: `mcpchrome`）を用意することを推奨します。
```bash
# 専用ユーザーの作成 (Linux)
sudo useradd -m -s /bin/bash mcpchrome

# 専用ユーザーで bridge を登録する場合
sudo -H -u mcpchrome npm install -g mcp-chrome-bridge
sudo -H -u mcpchrome mcp-chrome-bridge register
```

#### ④ Chrome 拡張機能（mcp-chrome-extension）の導入と初期接続
1. Bot 専用 Chrome プロファイルでブラウザを起動します（`./restart-ai.sh` を実行すると自動起動・構成されます）。
2. Chrome に `mcp-chrome` 拡張機能（未解凍拡張機能またはストア版）を読み込みます。
3. 拡張機能のアイコン/ポップアップを開き、**「Connect / Start」** をクリックしてネイティブサーバーを起動します。
4. ※ X (Twitter) などのログインが必要なサイトを調査対象にする場合は、この専用 Chrome プロファイル内で事前に一度だけ手動ログインを済ませておきます。

#### ⑤ 環境変数の設定 (`.env`)
`.env` に MCP サーバーの接続設定を追加・調整します。
```ini
# Chrome MCP サーバーのエンドポイント (デフォルト: http://127.0.0.1:12306/mcp)
WEB_RESEARCH_MCP_URL=http://127.0.0.1:12306/mcp
WEB_RESEARCH_MCP_TIMEOUT_SEC=20

# Linux サーバーで専用ユーザーを使う場合
MCP_CHROME_BOT_OS_USER=mcpchrome

# 未解凍拡張機能のパスを指定する場合 (省略時は ~/mcp-chrome-extension)
# MCP_CHROME_EXTENSION_PATH=/path/to/mcp-chrome-extension
```

#### ⑥ 接続テストの実行
付属の接続検証スクリプトを実行して、MCP サーバーおよび必須ツールが正常に応答するか確認します。
```bash
python3 mcp_chrome_connect_test.py
# 成功時の出力例: ok: initialized MCP session <session-id>; tools=9
```

---

## 🧪 型検査 (Type Checking)

コード品質を維持するため、Python 3.11+ の静的型付け（mypy）を採用しています。

```bash
mypy --config-file mypy.ini
```

---

## 📦 バージョン管理・GitHub Release

本リポジトリには GitHub Actions による自動リリースワークフロー（`.github/workflows/release.yml`）が組み込まれており、タグが push されると自動的にコミット履歴から更新内容を抽出して GitHub Releases を公開します。

### スクリプトで一括リリースする場合
`customize_for_github.py` の `--create-release` を使用することで、コミット作成からタグ付け・リモートへの Push までワンステップで実行できます：

```bash
python3 customize_for_github.py --create-release \
  --tag v1.0.0 \
  --title "Release v1.0.0: 初回公開" \
  --notes "・基本機能の公開\n・設定サンプルの提供"
```

### 通常の Git コマンドでリリースする場合
```bash
git commit -m "feat: 更新内容のメッセージ"
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin main
git push origin v1.0.0
```

---

## 📜 規約・ポリシー

- [利用規約 (Terms of Service)](TERMS_OF_SERVICE.md)
- [プライバシーポリシー (Privacy Policy)](PRIVACY_POLICY.md)

---

## 📄 ライセンス

このプロジェクトは [MIT License](LICENSE) のもとで公開されています。
