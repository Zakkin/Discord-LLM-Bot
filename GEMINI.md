# 🤖 Discord AI Bot ガイドライン (GEMINI.md)

本プロジェクト（`discord-ai-bot`）における設計前提、開発原則、および具体的な禁止事項（Ban List）は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md) に集約・定義されています。

作業にあたっては、必ず以下のドキュメントを参照・遵守してください：

- 🧭 **全体ガイドライン・禁止事項インデックス**: [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md)
- 🏛️ **設計前提・コア開発原則・チェックリスト**: [.agents/rules/principles.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/.agents/rules/principles.md)
- 🚫 **具体的禁止事項・バグ再発防止ナレッジ（全72項目）**: [.agents/rules/ban_list.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/.agents/rules/ban_list.md)

> [!IMPORTANT]
> **最優先原則の抜粋**:
> 1. **単一プロセス稼働**: 同一トークンでの複数プロセス同時起動および個別 `tree.sync()` の禁止。
> 2. **スコープ遵守**: 感情・関係値などの永続状態は `self` 経由で管理（`runtime.emotion_state` は存在しません）。
> 3. **多重防御・フォールバック**: 外部通信の包括的例外処理、LLM推論のサニタイズ・思考暴走抑止の徹底。
> 4. **静的型付けの厳格遵守**: Python 3.11+ 型アノテーションの付与と `mypy` 検査（エラー 0 件）の必須化。
