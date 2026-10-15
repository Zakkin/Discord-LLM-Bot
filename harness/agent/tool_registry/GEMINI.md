# 🤖 Tool Registry ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/tool_registry/`）は、AIエージェントの**フェーズ3（Action/ツール選択と実行）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/tool_registry/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/tool_registry/` 外）のファイルを直接編集してはならない。
> 2. **多重防御**: 外部通信（MCP / Web / Weather API）の完全な例外ハンドリングとタイムアウト制御を徹底すること。
> 3. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
