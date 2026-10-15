[← READMEに戻る](../README.md)

# 🌐 Chrome MCP サーバーの連携設定 (Web 自然調査)

本 Bot は **Chrome MCP (Model Context Protocol) サーバー** と連携することで、会話中の疑問や最新のネット評判、X (Twitter) タイムラインなどをブラウザ経由で自動調査できます。

---

## 📋 動作条件

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

---

## 🛠️ 準備すべきこと (セットアップ手順)

### ① Google Chrome または Chromium のインストール
- **Linux (Ubuntu/Debian)**: `sudo apt install -y google-chrome-stable` または `chromium-browser`
- **Linux (Arch)**: `sudo pacman -S google-chrome` または `chromium`
- **macOS**: `Google Chrome.app` をインストール

### ② `mcp-chrome-bridge` のインストールとマニフェスト登録
Chrome 拡張機能とネイティブホストを中継するブリッジツール（Node.js / npm）をインストールし、Native Messaging マニフェストを登録します。
```bash
# グローバルインストール
npm install -g mcp-chrome-bridge

# ネイティブホストマニフェストの登録
mcp-chrome-bridge register
```

### ③ 専用 OS ユーザーと Chrome プロファイルの準備 (Linux 推奨)
Linux サーバーで root 運用する場合、Chrome は root 実行を制限するため、専用ユーザー（例: `mcpchrome`）を用意することを推奨します。
```bash
# 専用ユーザーの作成 (Linux)
sudo useradd -m -s /bin/bash mcpchrome

# 専用ユーザーで bridge を登録する場合
sudo -H -u mcpchrome npm install -g mcp-chrome-bridge
sudo -H -u mcpchrome mcp-chrome-bridge register
```

### ④ Chrome 拡張機能（mcp-chrome-extension）の導入と初期接続
1. Bot 専用 Chrome プロファイルでブラウザを起動します（`./restart-ai.sh` を実行すると自動起動・構成されます）。
2. Chrome に `mcp-chrome` 拡張機能（未解凍拡張機能またはストア版）を読み込みます。
3. 拡張機能のアイコン/ポップアップを開き、**「Connect / Start」** をクリックしてネイティブサーバーを起動します。
4. ※ X (Twitter) などのログインが必要なサイトを調査対象にする場合は、この専用 Chrome プロファイル内で事前に一度だけ手動ログインを済ませておきます。

### ⑤ 環境変数の設定 (`.env`)
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

### ⑥ 接続テストの実行
付属の接続検証スクリプトを実行して、MCP サーバーおよび必須ツールが正常に応答するか確認します。
```bash
python3 mcp_chrome_connect_test.py
# 成功時の出力例: ok: initialized MCP session <session-id>; tools=9
```
