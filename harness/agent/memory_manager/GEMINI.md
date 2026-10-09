# 🧠 Memory Manager ガイドライン (GEMINI.md)

本パッケージ（`harness/agent/memory_manager`）における設計前提、開発原則、および具体的な禁止事項・多重防御ルールは、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/memory_manager/AGENTS.md) および親プロジェクトの [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md) に定義されています。

作業にあたっては、必ず以下のドキュメントを参照・遵守してください：

- 🧭 **メモリマネージャー ガイドライン・設計仕様**: [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/memory_manager/AGENTS.md)
- 📖 **パッケージ概要とモジュール構成**: [README.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/memory_manager/README.md)
- 🏛️ **親プロジェクト全体ガイドライン**: [親 AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md)

---

## ⚡ 最優先開発原則チェックリスト

AIエージェントが本ディレクトリのコードを変更・追加する際は、以下のチェック項目を必ず満たしているか確認してください：

1. **長期記憶ハルシネーション完全遮断（項目87）**:
   - 会話根拠のない事実や約束を捏造保存していないか（`_is_unsupported_hallucinated_memory`）。
   - 感情理由（`emotion_reason`）からの引き写し記憶を排除しているか（`_is_hallucinated_emotion_reason_memory`）。
   - AI自身のプロフィールや返答ポリシーをユーザー記憶として保存していないか（`_is_self_referential_memory`）。
2. **テーブル整合性と3重同期（項目106）**:
   - `memories`、`vec_memories`（sqlite-vec）、`fts_memories`（FTS5）の追加・更新・削除が完全に連動しているか。
3. **トピック隔離と自然なRAG想起（項目85）**:
   - 自立したユーザーの疑問・質問に対して直前AI発言を無理に検索クエリへ引きずっていないか。
4. **第三者呼称の誤学習防止（項目93）**:
   - 定期内省（`reflection.py`）で会話ログ内の別人の名前を相手の呼び名として保存していないか（`is_reflection_belief_valid`）。
5. **50KB制限の厳守（項目100）**:
   - 単一ファイルが肥大化していないか（全モジュール 50KB 未満を維持）。
6. **非同期・排他制御とエラーハンドリング**:
   - SQLite接続リース（`_cursor`）を適切に使用し、`sqlite-vec` / `FTS5` ロード失敗時もフォールバックで安全に稼働するか。
7. **型安全性**:
   - Python 3.11+ 型アノテーションが完備され、`mypy` でエラーが発生しないか。
