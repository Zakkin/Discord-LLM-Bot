# 🤖 Discord Bot アプリケーション層 ガイドライン (GEMINI.md)

本ディレクトリ（`ollama_bot/`）は、**Discord APIとの入出力、UI層（スラッシュコマンド・ContextMenu・ボタン）、Presence同期、およびハーネスへの純粋なファサード**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/ollama_bot/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`ollama_bot/` 外）のファイルを直接編集してはならない。
> 2. **単一プロセス稼働**: 同一トークン複数プロセス起動および重複 `tree.sync()` を固く禁じる。
> 3. **API整合性**: `super().on_interaction` 呼び出し禁止、`IGNORE_USER_IDS` 全遮断、コンフィグ駆動設計の徹底。
> 4. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
