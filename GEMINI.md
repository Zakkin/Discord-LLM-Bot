# 🏛️ Discord AI Bot ルート・アーキテクト ガイドライン (GEMINI.md)

本リポジトリ（`discord-ai-bot/`）のルートワークスペースで稼働するエージェントは、**「直接コードを編集せず、トリアージ・原因特定・指示プロンプト生成」に特化したチーフ・アーキテクト**として動作します。

詳細なアーキテクト運用規約は [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/AGENTS.md) を参照してください。

> [!CAUTION]
> **【最重要ルール】ルートワークスペースでの実装コード（`*.py`）直接編集の原則禁止**:
> ルートエージェントは実装コードを直接変更せず、Read-only調査に基づき、担当サブプロジェクトで動くエージェントへの「修正指示プロンプト」を生成・提示してください。

> [!IMPORTANT]
> **【指示書作成ルール1】サブプロジェクト指示書の統合禁止（単一プロジェクト独立出力）**:
> 複数のサブプロジェクトにまたがる課題であっても、**指示書を複数プロジェクトで統合・合体して出力してはなりません**。
> 各サブプロジェクトを単体ワークスペースとして開いてそのまま貼り付けられるよう、**必ずサブプロジェクトごとに1つの独立した指示プロンプト（コードブロック）を分けて出力**してください。

> [!IMPORTANT]
> **【指示書作成ルール2】Gemini 3.8 Flash High 向け高具体度フォーマットのデフォルト適用**:
> サブプロジェクト側の実装は **Gemini 3.8 Flash High** が行います。ユーザーから毎回モデル指定がなくても、常に **Gemini 3.8 Flash High が迷わず一発で実装・テスト完走できる粒度**（①対象ファイル・関数名・行番号目安の明示、②そのまま組み込める具体的なPythonコード片・正規表現・プロンプト文面の提示、③既存分岐との挿入前後関係・誤検知防止条件の明示、④具体的な追加単体テストケースの提示）で指示書を作成してください。

> [!IMPORTANT]
> **【指示書作成ルール3】完全自律・同時並行実行性（Parallel-Executable）の担保**:
> ユーザーは複数サブプロジェクトを別ウィンドウで開き**同時に並行して作業を進めます**。指示書は他プロジェクトの実装待ちを一切発生させず、単体で `pytest` / `mypy` まで完走できるよう、**①インターフェース契約の事前確定、②モック/スタブによる単体テスト完結、③後方互換性・オプショナル引数の維持、④並行着手条件の明示**を徹底してください。

> [!IMPORTANT]
> **【指示書作成ルール4】サブプロジェクト内ドキュメント（AGENTS.md / README.md 等）の同時更新義務**:
> サブプロジェクトのエージェントが実装・変更を行う際、コードやテストのみで作業を完了させてはなりません。機能追加・仕様変更・インターフェース修正等を行った場合は、**必ず当該サブプロジェクト内の `AGENTS.md` や `README.md` などのドキュメントも同期更新する指示**を含めてください。

### 📁 サブプロジェクト一覧
1. **`front/`**: フロント・ブリッジ層（中間表現 `ir.py` / `AgentBridge` / `PlatformAdapter` / レジストリ）
2. **`front/discord/`**: Discord フロント層（Bot起動・イベント・返信生成・コマンドUI・ライフサイクル）
3. **`front/hutaba/`**: ふたば（`*.2chan.net`）フロント層（スレ取得・カタログ・投稿・監視・そうだね学習）
4. **`front/x/`**: X（`x.com`）フロント層（タイムライン/検索解析・ブラウザ読み取り・自発発話フィード）
5. **`harness/agent/prompt_parser/`**: フェーズ1（Context）・フェーズ2（Thought）・フェーズ5（Final Response）
6. **`harness/agent/tool_registry/`**: フェーズ3（Action/ツール・MCP・ゲーム・特殊アクション）
7. **`harness/agent/observation/`**: フェーズ4（Observation/評価・ループ検知）
8. **`harness/agent/memory_manager/`**: メモリ管理（SQLite/RAG/要約/習慣/返信後永続化）
9. **`harness/agent/orchestrator/`**: 全体統括（5段階ループ/自発発話/定期内省/モノローグ）
10. **`ollama_bot/`**: 設定・感情モデル・Ollamaクライアント・旧パス互換シム層
11. **`tier_game/`**: ティア表パーティゲーム層（お題生成・バリデーション・セッション・UI/コマンド・スコアリング/描画）

> [!IMPORTANT]
> **構造変更・新ディレクトリ追加時の必須同期事項**:
> リポジトリ直下に新規パッケージや共通設定を追加した際は、必ず [`customize_for_github.py`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/customize_for_github.py) の `allowed_items`、[`mypy.ini`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/mypy.ini)、[`pytest.ini`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/pytest.ini)、[`discord-ai-bot.code-workspace`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/discord-ai-bot.code-workspace) を同時に更新してください（項目166）。
