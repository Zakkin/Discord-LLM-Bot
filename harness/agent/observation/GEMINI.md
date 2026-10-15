# 🤖 Observation ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/observation/`）は、AIエージェントの**フェーズ4（Observation/結果の観察と評価）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/observation/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/observation/` 外）のファイルを直接編集してはならない。
> 2. **ループ検知とトークン管理**: 同一ツール重複呼び出しや連続エラーを確実に遮断し、推論コンテキスト肥大化を防ぐこと。
> 3. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
