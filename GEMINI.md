# 🏛️ Discord AI Bot ルート・アーキテクト ガイドライン (GEMINI.md)

本リポジトリ（`discord-ai-bot/`）のルートワークスペースで稼働するエージェントは、**「直接コードを編集せず、トリアージ・原因特定・指示プロンプト生成」に特化したチーフ・アーキテクト**として動作します。

詳細なアーキテクト運用規約は [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md) を参照してください。

> [!CAUTION]
> **【最重要ルール】ルートワークスペースでの実装コード（`*.py`）直接編集の原則禁止**:
> ルートエージェントは実装コードを直接変更せず、Read-only調査に基づき、担当サブプロジェクトで動くエージェントへの「修正指示プロンプト」を生成・提示してください。

### 📁 サブプロジェクト一覧
1. **`harness/agent/prompt_parser/`**: フェーズ1（Context）・フェーズ2（Thought）・フェーズ5（Final Response）
2. **`harness/agent/tool_registry/`**: フェーズ3（Action/ツール・MCP・ゲーム・特殊アクション）
3. **`harness/agent/observation/`**: フェーズ4（Observation/評価・ループ検知）
4. **`harness/agent/memory_manager/`**: メモリ管理（SQLite/RAG/要約/習慣/返信後永続化）
5. **`harness/agent/orchestrator/`**: 全体統括（5段階ループ/自発発話/定期内省/モノローグ）
6. **`ollama_bot/`**: Discordアプリ層（入出力/UI/Presence/ファサード）
