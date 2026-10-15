# 🤖 Prompt Parser ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/prompt_parser/`）は、AIエージェントの**フェーズ1（Context構築）・フェーズ2（Thought/計画立案）・フェーズ5（Final Response生成）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/prompt_parser/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/prompt_parser/` 外）のファイルを直接編集してはならない。
> 2. **出力サニタイズ**: 思考タグ除去、思考サルベージ、相手呼称誤認防止、オウム返し誤爆防止の多重防御を死守すること。
> 3. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
