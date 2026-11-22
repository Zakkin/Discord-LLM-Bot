[← READMEに戻る](../README.md)

# 🌐 Chrome MCP プロキシサーバー連携設定 (Web 自然調査)

本 Bot はリポジトリ内蔵の **Chrome MCP プロキシサーバー**（[`chrome_mcp_server.py`](../chrome_mcp_server.py) → [`harness/agent/tool_registry/mcp/chrome/server.py`](../harness/agent/tool_registry/mcp/chrome/server.py)）と連携し、Playwright が自前で起動するヘッドレス Chromium を用いて Web 調査・ページ本文抽出を行います。

> [!NOTE]
> 現行方式は FastAPI + uvicorn + Playwright による **内蔵プロキシ方式** です。外部の Chrome 拡張機能、ネイティブホスト（ブリッジ）、専用 OS ユーザー、および GUI / X ディスプレイ環境はいずれも不要です。

---

## 🔌 接続契約

1. **MCP HTTP エンドポイント**:
   - Bot 側設定: `WEB_RESEARCH_MCP_URL`（既定 `http://127.0.0.1:12306/mcp`、[`ollama_bot/config_base.py`](../ollama_bot/config_base.py)）
   - プロキシ側待受: `127.0.0.1:12306`（`server.py` / `chrome_mcp_server.py` に固定で直書き、`POST /mcp` の JSON-RPC 2.0 に対応）
   - タイムアウト設定: `WEB_RESEARCH_MCP_TIMEOUT_SEC`（既定 `20` 秒）
2. **プロキシが提供する MCP ツール（`tools/list`）と Bot 側フィルタ**:
   - プロキシ提供ツール:
     - `chrome_navigate`: 指定 URL へ遷移し、`tabId` を発行・保持
     - `chrome_get_web_content`: 指定 `tabId` または `url` から本文テキスト・タイトルを抽出
     - `chrome_search`: ヘッドレス Chromium 上で検索を実行し、結果一覧を取得
     - `chrome_screenshot`: 指定 Web ページのスクリーンショットを取得（`describe=True` 時は Vision モデルで画面解析後に VRAM 解放）
   - Bot 側安全フィルタ（従来通り）:
     - クライアント（`MCPChromeHTTPClient`）は `WEB_RESEARCH_MCP_ALLOWED_TOOLS` の許可リストに含まれ、かつ `WEB_RESEARCH_MCP_BLOCKED_TOOL_KEYWORDS`（`click`, `press`, `type`, `fill`, `submit`, `download`, `upload`, `delete`, `remove`, `purchase`, `buy`, `checkout`, `cart`, `payment` 等）を含まないツールのみを実行します。入力・送信・購入操作系ツールは Bot 側で自動的にブロック・除外されます。
3. **セッションファイル・排他ロック**:
   - Bot 側はセッションファイル `/tmp/discord-ai-bot-chrome-mcp-session`（`WEB_RESEARCH_MCP_SESSION_PATH`、`config_base.py` / `web_research/config.py`）およびブラウザ操作ロック `/tmp/discord-ai-bot-browser.lock`（`WEB_RESEARCH_BROWSER_LOCK_PATH`）を使用します。
   - ※ 現行プロキシ自体はステートレスにリクエストを処理し `Mcp-Session-Id` ヘッダを発行しないため、ファイル未作成の状態でも動作に支障はありません。

---

## 👥 利用者（共有経路と単一常駐）

- **Discord の Web 調査**: `img`・`kita` をはじめとする各 Bot インスタンスが同一の `http://127.0.0.1:12306/mcp` を共有し、ファイルロック（`/tmp/discord-ai-bot-browser.lock`）で排他制御しながら利用します。
- **ふたばの MCP 経由スレ返信**: [`front/hutaba/client/img2channet_parts/30_submission.py`](../front/hutaba/client/img2channet_parts/30_submission.py) の `_submit_img_thread_reply_via_mcp` も同じエンドポイントを参照します。
- **単一常駐**: すべての MCP 経路が同じプロキシを向くため、ホスト上に常駐させるプロセスは **1 つ** だけで全経路をまかなえます（ポート `12306` 固定）。なお、[`deploy/chrome-cdp.service`](../deploy/chrome-cdp.service)（ポート `9222` の CDP 用 Chromium）とは別系統です。

---

## 🛠️ セットアップ手順

### ① `venv` への依存パッケージ・Chromium 導入
`requirements.txt` / `requirements.lock` にはプロキシ用パッケージが含まれていないため、`venv` へ必須の 3 パッケージと Chromium ブラウザ実体を導入します：
```bash
venv/bin/pip install fastapi uvicorn playwright
venv/bin/python -m playwright install chromium
```

> [!IMPORTANT]
> - **3 パッケージの一括必須性**: [`server.py`](../harness/agent/tool_registry/mcp/chrome/server.py) 冒頭は `fastapi`・`uvicorn`・`playwright` の 3 つの `import` を一括判定しており、1 つでも欠けると例外を出さずにダミークラスへ差し替えて起動を継続（劣化起動）し、全リクエストに `500` を返すようになります。必ず 3 つすべてを揃えてください。
> - **Playwright とブラウザ実体の版一致**: Playwright のパッケージ版と Chromium ブラウザ実体の版は一致が必須です。Playwright を更新した場合は `venv/bin/python -m playwright install chromium` を必ず再実行してください。
> - **導入・更新後の再起動必須**: パッケージやブラウザ実体をインストール・更新した後は、必ずサービスを再起動してください（起動中のプロセスは古い状態や劣化状態を保持し続けます）。

### ② systemd サービスの配置と起動
[`deploy/chrome-mcp-proxy.service`](../deploy/chrome-mcp-proxy.service) を配置して常駐化します（`rukbat` 以外の環境へ配置する場合はユニット内の `WorkingDirectory` と `ExecStart` のパスを環境に合わせて調整してください）：
```bash
sudo cp deploy/chrome-mcp-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chrome-mcp-proxy
```
- パッケージ導入・更新や設定変更後に反映する際は `sudo systemctl restart chrome-mcp-proxy` を実行してください。
- Bot 本体は調査の都度 `WEB_RESEARCH_MCP_URL` へ接続するため、プロキシの起動・再起動に伴う **Bot 本体の再起動は不要** です。

### ③ 環境変数の設定 (`.env`)
必要に応じて `.env` で Bot 側の接続設定を調整します（未設定時は既定値が使われます）。
```ini
# Chrome MCP プロキシのエンドポイント (デフォルト: http://127.0.0.1:12306/mcp)
WEB_RESEARCH_MCP_URL=http://127.0.0.1:12306/mcp
WEB_RESEARCH_MCP_TIMEOUT_SEC=20
WEB_RESEARCH_MCP_SESSION_PATH=/tmp/discord-ai-bot-chrome-mcp-session
# ブラウザ調査を有効化する場合 (デフォルト: true)
# WEB_RESEARCH_BROWSER_ENABLED=true
```
※ 旧方式で使用されていた専用ユーザー・拡張機能パス関連の環境変数は現行コードから参照されません。

---

## 🔍 確認方法

1. **(a) サービス稼働・journal 起動完了ログの確認**:
   ```bash
   sudo systemctl status chrome-mcp-proxy
   journalctl -u chrome-mcp-proxy -n 50 --no-pager
   ```
   - journal に **`[tool_registry] Playwright browser launched.`** と **`Application startup complete`** の両方が出力されていることを確認します。
2. **(b) 待受ポート確認と素の GET 応答の確認（405 が正常、500 は異常）**:
   ```bash
   ss -ltnp | grep 12306
   curl -i http://127.0.0.1:12306/mcp
   ```
   - `ss` で `127.0.0.1:12306` が LISTEN 状態であることを確認します。
   - `/mcp` は `POST` 専用ルートであるため、素の GET に対する正常時の応答は **`405 Method Not Allowed`** です。**全リクエストで `500` が返る場合は劣化起動のサインであり、正常ではありません**（「待ち受けている＝正常」と誤認しないこと）。
3. **(c) 決定的な確認（`POST initialize` の疎通）**:
   以下のコマンドで `initialize` を送信し、`protocolVersion` を含む JSON 応答が返ることを確認します：
   ```bash
   curl -sS -X POST http://127.0.0.1:12306/mcp \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"clientInfo":{"name":"check","version":"1.0"}}}'
   ```
   正常時の応答例:
   ```json
   {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}}}}
   ```
4. **(d) 最終確認（Bot ログ）**:
   - Bot のログ（例: `logs/bot_<config名>.log`）で MCP リクエストがエラーなく完了し（`MCP request completed: ... status=200` 等）、Web 調査がブラウザ経路（`source_backend=chrome_mcp` 等）で結果を返すことを確認します。

---

## ⚠️ 停止時の挙動

- **Discord の Web 調査**: プロキシが停止していると `127.0.0.1:12306` への接続に失敗し、単純な HTTP 直接検索（DuckDuckGo Lite POST 等）へフォールバックします（動的描画やページ本文精査が効かないため検索の質は低下します）。URL 直接読み取り（`browser_read_url`）等も失敗します。
- **ふたばの MCP 経由スレ返信**: [`_submit_img_thread_reply_via_mcp`](../front/hutaba/client/img2channet_parts/30_submission.py) は接続失敗時に `WARNING` ログ（`[hutaba] submit_img_thread_reply_via_mcp failed ...`）を出力して失敗し、直接 HTTP 投稿へフォールバックします（※なお現行プロキシは入力・クリック系ツールを提供していないため、プロキシ稼働時でもツール不足により直接 HTTP 投稿へフォールバックします）。
- **エラー文言に関する注意**: Bot 側（[`web_research/dispatch.py`](../harness/agent/tool_registry/web_research/dispatch.py)）の接続不可エラー文に「拡張機能が未接続」と表示されますが、これは汎用文言であり、現行方式に拡張機能は存在しません（プロキシプロセスの未起動・停止を指します）。

---

## 🚧 制限事項（プロファイル・ログイン状態）

- 現行プロキシ（`server.py`）はリクエストごとに Playwright の素のコンテキスト（`browser.new_context()`）を新規作成・破棄するため、Cookie やサイトへのログイン状態は保持しません。
- そのため、X（`x.com`）のタイムライン取得など **ログイン前提の調査はできません**（ログイン壁により失敗します）。
- （注記）旧方式の専用ブラウザプロファイルは、事前に手動ログインした状態を保持してこうしたサイトを調査するためのものでした。

---

## 🛠️ トラブルシューティング

2026-11-22 に本番環境（`rukbat`）で発生・復旧した実障害に基づく代表的な症状・原因・対処です。

### (a) 全リクエストが 500 になり、journal に `TypeError: 'FastAPI' object is not callable` が出る
- **原因**: `server.py` 冒頭の `fastapi` / `uvicorn` / `playwright` 一括 `import` が失敗し、例外を出さずダミークラスへ差し替わったまま劣化起動している（パッケージ導入前に起動したプロセスが再起動されずに残っている場合など）。
- **対処**: `venv` の import を確認（不足があれば `venv/bin/pip install fastapi uvicorn playwright` で導入）し、サービスを再起動します。
  ```bash
  venv/bin/python -c "import fastapi, uvicorn, playwright"
  sudo systemctl restart chrome-mcp-proxy
  ```

### (b) 起動に失敗し、journal に `Executable doesn't exist at .../chromium_headless_shell-...` が出る
- **原因**: Playwright のパッケージ版と Chromium ブラウザ実体の版がずれている（または Chromium 実体が未導入）。
- **対処**: `playwright install chromium` を再実行してブラウザ実体を揃え、サービスを再起動します。
  ```bash
  venv/bin/python -m playwright install chromium
  sudo systemctl restart chrome-mcp-proxy
  ```

### (c) 接続拒否（`Connection refused`）になる
- **原因**: `chrome-mcp-proxy` サービスが未起動または停止している。
- **対処**: `systemctl status` と `journalctl` から原因を確認し、サービスを起動・有効化します。
  ```bash
  sudo systemctl status chrome-mcp-proxy
  journalctl -u chrome-mcp-proxy -n 50 --no-pager
  sudo systemctl enable --now chrome-mcp-proxy
  ```
