# 🤖 Orchestrator ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/orchestrator/`）は、AIエージェントの**オーケストレーション（5段階ループ統括・自発発話・自律制御）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/orchestrator/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/orchestrator/` 外）のファイルを直接編集してはならない。
> 2. **自発発話確率の保全**: 1通ごとの独立確率抽選（サイコロ方式）と重複エンキュー抑止を死守すること。
> 3. **非同期リソース管理**: 定期内省コルーチンの安全クローズとワーニング根絶を徹底すること。
> 4. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
