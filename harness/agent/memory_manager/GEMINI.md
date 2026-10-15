# 🤖 Memory Manager ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/memory_manager/`）は、AIエージェントの**メモリ管理（短期記憶・長期記憶/RAG・要約保存）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/memory_manager/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/memory_manager/` 外）のファイルを直接編集してはならない。
> 2. **ハルシネーション遮断**: 感情理由や別文脈からの事実捏造保存を事前ガードで徹底的に防止すること。
> 3. **アトミック書き込み**: DB永続化時の整合性と耐障害性を死守すること。
> 4. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
