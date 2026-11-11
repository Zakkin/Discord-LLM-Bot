# 🌉 Front Bridge サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`front/` 直下）は、各外部プラットフォーム（`front/discord/`, `front/hutaba/`, `front/x/`）と AI エージェント本体（`harness/agent/`）を橋渡しする**中間表現（IR）・ブリッジ層**サブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **中間表現（`front/ir.py`）**:
   - `Location` / `FrontEvent`（プラットフォーム入力・チャンネル情報の正規化、年齢制限 `is_nsfw` フラグ対応）、`AgentTask`（`AgentOrchestrator` へ渡す中間コード、コンテキストへの `is_nsfw` 伝搬）、`AgentOutcome`（推論結果）、`FrontAction`（プラットフォーム出力アクション）の型定義と JSON シリアライズ。
2. **プラットフォームアダプター規約（`front/adapter.py`）**:
   - 各プラットフォームが実装する `PlatformAdapter` Protocol の定義。
3. **エージェントブリッジ（`front/bridge.py`）**:
   - `AgentTask` を受け取り、`AgentOrchestrator.run(...)` 互換ランナーへ受け渡して `AgentOutcome` / `FrontAction` へ変換する `AgentBridge`。
4. **レジストリ（`front/registry.py`）**:
   - プラットフォームアダプターおよび `tool_registry` 向けドメインヘルパー（`x`, `hutaba`）の登録・遅延ロード。

### 📜 全フロント共通行動ログ（自分の体験の台帳: `bot_activities`）
- **目的:** 各フロント（Discord, ふたば, X）での発言・リアクション・タイムライン観察を単一の台帳に記録し、どの場所で聞かれても自己体験として一貫して回答可能にする。
- **書き込み規則:**
  - Discord: 通常返信 (`reply`)、相槌/スコアリアクション (`reaction`)
  - ふたば: スレッド書き込み成功時 (`post`)
  - X: 自発投稿向けのタイムライン観察 (`observed`)、オーケストレータ発話 (`reply`/`post`)
- **境界制約:**
  - 他者の発言内容は最大80文字（`ACTIVITY_TARGET_TEXT_MAX_CHARS`）で要旨としてのみ記録し、逐語引用を禁止する。
  - 保持期間は180日とし、定期ループで安全にパージする。
  - `ACTIVITY_LOG_ENABLED=false` で記録・検索を完全停止する。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `front/` 直下のファイル（`ir.py`, `adapter.py`, `bridge.py`, `registry.py`, `tests/`）のみを編集すること。**
   - 個別プラットフォーム（`front/discord/`, `front/hutaba/`, `front/x/`）や `harness/agent/*`, `ollama_bot/` のファイルを直接編集してはならない。
2. **【`front/` を直接 `PYTHONPATH` / `sys.path` に入れることの厳禁】**:
   - `front/discord` ディレクトリが存在するため、`front/` 自体を `PYTHONPATH` に追加すると外部ライブラリ `discord.py`（`import discord`）が隠蔽され起動不能になる。`PYTHONPATH` は常にリポジトリルート（`.` および `mock_libs`）とすること。
3. **プラットフォーム非依存の維持**:
   - `front/ir.py`, `front/adapter.py`, `front/bridge.py` から `discord` や特定プラットフォーム固有モジュールを直接 import しないこと。
4. **【公開・デプロイスクリプトとの同期義務（項目166）】**:
   - リポジトリ直下に新規パッケージやサブプロジェクト（`front/` 配下含む）を追加・再編した際は、必ず [`customize_for_github.py`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/customize_for_github.py) の `allowed_items`（エクスポート許可リスト）に登録されていることを確認すること（未登録だとGitHub公開や本番サーバへの同期で `ModuleNotFoundError` が発生する）。

---

## 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/tests
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front
```
