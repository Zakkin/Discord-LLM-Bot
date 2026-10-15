# 🤖 Discord AI Bot

ローカル LLM (Ollama / llama-server) やクラウド API をバックエンドに備えた、**日本語での自然な対話・キャラクター表現に特化**した高機能かつ自律的な Discord AI チャット Bot です。  
感情シミュレーション、長期的エピソード記憶、ユーザー関係値モデル、Chrome MCP による Web 調査、話題乗っ取り防止（Anti-Hijack）ツリー保護などを統合し、日本語コミュニティの複数人雑談でも滑らかで人間味のある会話を実現します。

---

## 📖 概要

### ✨ 主な機能
- 🇯🇵 **自然な日本語対話・キャラクター表現特化**:
  - 相槌、ツッコミ、タメ口・敬語・ネットスラングなど多様なペルソナに対応。
  - 全角半角正規化、日本語境界判定、オウム返し抑止など、複数人同時雑談に最適化。
- 🎼 **自律型 ReAct ハーネス・アーキテクチャ**:
  - 5段階（Context ➔ Thought ➔ Action ➔ Observation ➔ Final Response）の自律推論・行動ループ。
  - 軽量分類モデル（Middle-LLM）による事前意図判定（Tool Routing Gate）で日常会話は低遅延で即答。
- 💖 **動的感情状態 & ユーザー関係値システム**:
  - 喜怒哀楽パラメータが動的変動し、ユーザーごとの親愛度・信頼度（関係値）を学習・蓄積。
  - アトミック書き込みによる永続化と、時間経過による感情減衰（Neutral回帰）。
- 📚 **ハイブリッド長期記憶システム (MemoryStore)**:
  - SQLite (FTS5 全文検索) + `sqlite-vec` (ベクトル検索) による高精度 RAG。
  - 記憶検索 ➔ Web自然調査 ➔ 不知表明の3段階フォールバック。架空事実捏造（ハルシネーション）を自動遮断。
- 🛡️ **会話ツリー分離 & Anti-Hijack 機構**:
  - 返信（Reply）時に親メッセージを最優先文脈として保護し、並行雑談からの話題乗っ取りを防止。
- 🌐 **Web 自然調査 & 国内気象情報 MCP (Model Context Protocol)**:
  - Chrome MCP 連携により、会話中の疑問や評判をブラウザ経由で自然に調査。
  - 気象庁（JMA）・ウェザーニュース連携による国内気象情報取得。
- 🎮 **ティア表パーティゲーム（/tier_game）**:
  - S〜Dランク付けを題材にした心理・価値観当てパーティゲーム Cog を内蔵。

---

## 🛠️ 使用している主な技術

| 分野 | 主な技術・ライブラリ | 用途・詳細 |
| :--- | :--- | :--- |
| **言語・ランタイム** | Python 3.11+ | 非同期処理（`asyncio`）、厳格な型アノテーション（`mypy` 0エラー運用） |
| **Bot フレームワーク** | `discord.py` 2.x | Discord API 連携、スラッシュコマンド、UIコンポーネント（ボタン・モーダル） |
| **LLM バックエンド** | **Ollama** / **llama-server**<br>**OpenAI API** / **Google Gemini API** | ローカル推論およびクラウド API 連携（Speculative Decoding、OpenAI 互換モード） |
| **推論モデル** | `Qwen3.6` / `Qwen2.5` / `Qwen3.5`<br>`bge-m3` / `gpt-4o` / `gemini-2.0-flash` | 会話生成、事前意図判定（Middle-LLM）、ベクトル埋め込み、画像認識 |
| **データベース・検索** | **SQLite 3** + **FTS5** + **`sqlite-vec`** | ハイブリッド長期記憶（全文検索＋ベクトル類似度検索 RAG）、エピソード蓄積 |
| **外部ツール連携** | **Model Context Protocol (MCP)**<br>Chrome DevTools / Native Messaging | Chrome MCP サーバー連携（Web自然調査）、国内気象データ取得 |
| **品質保証・テスト** | **pytest**, **mypy**, **ruff** | 900件超の単体・統合テストスイート、静的型検査、自動サニタイズ |

---

## 🚀 環境構築手順

### 1. 必要要件
- **Python**: 3.11 以上 (3.11 〜 3.14 対応)
- **LLM 推論環境**:
  - **ローカル推論**: **Ollama** または **llama-server**（GPU 環境推奨）
  - **クラウド API**: **OpenAI API** または **Google Gemini API**
- **Discord Bot Token**: Discord Developer Portal から取得
- **Google Chrome / Chromium & Node.js** (任意): Web 自然調査（Chrome MCP）を利用する場合

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

### 3. 環境設定 (.env)
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

# メイン応答チャンネル ID (全発言に応答)
OLLAMA_CHANNEL_ID=123456789012345678

# サブチャンネル ID (カンマ区切りで複数指定可・確率応答)
OTHER_CHANNEL_IDS=123456789012345679,123456789012345680

# Bot 管理者ユーザー ID (カンマ区切り・稼働制御用)
BOT_ADMIN_USER_IDS=123456789012345678

# --- 推論バックエンド設定 ---
# A. ローカル Ollama 運用の例:
OLLAMA_BASE_URL=http://127.0.0.1:11434

# B. クラウド API (OpenAI / Gemini) 運用の例:
# OLLAMA_USE_OPENAI_API=true
# OLLAMA_BASE_URL=https://api.openai.com
# OLLAMA_MODEL=gpt-4o
# OLLAMA_MIDDLE_MODEL=gpt-4o-mini
```

### 4. キャラクター設定ファイルの作成
`config_sample.py` をコピーして、専用のキャラクター設定ファイル（例: `config_mybot.py`）を作成します。

```bash
cp ollama_bot/config_sample.py ollama_bot/config_mybot.py
```
`config_mybot.py` を開き、Botの名前、一人称、口調、コアバリュー等を自由にカスタマイズしてください。

### 5. Modelfile の作成 (Ollama 利用時)
`modelfiles/` ディレクトリに用意されている Modelfile から中間モデル等を作成します。

```bash
ollama create middle-llm -f modelfiles/middle-llm.Modelfile
ollama create classifier-local-v2 -f modelfiles/classifier-v2.Modelfile
```

### 6. Bot の起動

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

## 🔧 トラブルシューティング

### 1. Bot がメッセージに反応しない
- **Privileged Gateway Intents の確認**:
  - Discord Developer Portal の **[Applications] ＞ 該当Bot ＞ [Bot]** タブで、`Message Content Intent` と `Server Members Intent` が **ON** になっているか確認してください。OFF の場合、Bot は発言内容を読み取れません。
- **チャンネル設定の確認**:
  - 発言しているチャンネルが `.env` の `OLLAMA_CHANNEL_ID`（常時応答）または `OTHER_CHANNEL_IDS`（確率応答）に含まれているか確認してください。
  - サブチャンネルの場合、メンション（`@Bot`）を付けて反応するか試してください（メンション時は確率抽選をバイパスして即時応答します）。
- **Bot 権限の確認**:
  - 対象チャンネルで Bot に `View Channels`、`Send Messages`、`Read Message History` が許可されているか確認してください。

### 2. Ollama / LLM 接続エラー・タイムアウトが発生する
- **エンドポイントの疎通確認**:
  - `curl http://127.0.0.1:11434/api/version` を実行し、Ollama が正常に応答するか確認してください。
- **モデルの存在確認**:
  - `ollama list` を実行し、`.env` や `config_*.py` で指定したモデル名（`OLLAMA_MODEL`, `OLLAMA_MIDDLE_MODEL` 等）がローカルに存在するか確認してください。
- **タイムアウト設定**:
  - 推論に時間がかかる環境では、`.env` で `OLLAMA_REQUEST_TIMEOUT_SEC`（デフォルト: 180秒）を長めに設定してください。

### 3. ニックネームの変更に失敗する / 権限エラーが出る
- **ロール階層の確認**:
  - Discord サーバー設定の **[ロール]** 一覧で、**Bot のロールが一般メンバーのロールより上位** に配置されている必要があります。Bot より上位のロールを持つユーザーのニックネーム変更や、Bot 自身のロールが最下位にある場合は変更できません。
- **権限の確認**:
  - Bot に `Change Nickname`（ニックネームの変更）権限が付与されているか確認してください。

### 4. Chrome MCP 連携（Web 自然調査）がエラーになる
- **ブリッジの登録確認**:
  - `npm install -g mcp-chrome-bridge && mcp-chrome-bridge register` が完了しているか確認してください。
- **拡張機能の接続状態**:
  - Chrome を起動し、`mcp-chrome` 拡張機能のポップアップで **「Connect / Start」** が押されているか確認してください。
- **接続テストの実行**:
  - `python3 mcp_chrome_connect_test.py` を実行して疎通と提供ツールを確認してください。

### 5. スラッシュコマンド（`/tier_game`, `/bot` 等）が表示されない
- **OAuth2 スコープの確認**:
  - Bot 招待時に `applications.commands`（または `bot`）スコープが含まれていたか確認してください。
- **反映遅延**:
  - グローバルコマンドは反映に時間がかかる場合があります。特定のテストサーバーで即時同期させたい場合は、`.env` の `DISCORD_GUILD_ID` を指定してください。

### 6. 多重起動による二重返信やデータベースのロック
- **単一プロセスの確認**:
  - 同一の Bot トークンで複数のプロセスが同時に起動していると、メッセージへの二重応答や SQLite データベースの競合エラーが発生します。
  - プロセスを確認・停止するには `./restart-ai.sh` を利用するか、`ps aux | grep ollama_bot` で既存プロセスを停止してください。

---

## 📚 詳細ドキュメント

より詳しい設計・設定ガイドは以下のドキュメントを参照してください：

- 📁 [システムアーキテクチャ & ディレクトリ構成](docs/architecture.md): レイヤード構造、ReAct 5段階フロー、詳細ディレクトリツリー
- 🧠 [LLM モデル構成 & 推奨スペック](docs/models.md): マルチモデル運用、推奨パラメータ規模、VRAM目安
- ☁️ [外部 LLM API 連携 (OpenAI / Google Gemini)](docs/external_api.md): クラウド API 接続設定、ローカル・クラウドハイブリッド運用
- ⚙️ [Discord Bot 設定 & 権限・チャンネル設定](docs/discord_setup.md): インテント、ロール階層、チャンネル応答モード、稼働制御コマンド
- 🌐 [Chrome MCP サーバー連携設定 (Web 自然調査)](docs/chrome_mcp.md): ブラウザ自動調査のセットアップ手順、動作要件

---

## 📜 規約・ポリシー

- [利用規約 (Terms of Service)](TERMS_OF_SERVICE.md)
- [プライバシーポリシー (Privacy Policy)](PRIVACY_POLICY.md)

---

## 📄 ライセンス

このプロジェクトは [MIT License](LICENSE) のもとで公開されています。
