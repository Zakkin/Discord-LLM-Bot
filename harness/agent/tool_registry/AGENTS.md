# 🤖 Tool Registry サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/tool_registry/`）は、AIエージェントの**フェーズ3（Action/ツール選択と実行）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **ツールレジストリと実行基盤**:
   - `ToolRegistry`, `ToolExecutor`, `ToolDefinition`, `ToolParameter` によるスキーマ管理と実行制御。
   - `ToolCategory`（`WEB`, `MCP`, `WEATHER`, `SYSTEM`, `CUSTOM`）によるツールの明確な分類とライフサイクル管理。
2. **MCPおよび外部ツール連携**:
   - Chrome Browser MCP, ふたば（img2chan）MCP, X (Twitter) タイムライン MCP, 天気（Weather）API。
   - 外部Web調査（Search / Extract）および事実検証（Fact Check）。
3. **ゲーム・特殊アクションアダプター**:
   - 特殊アクションアダプター（`adapters/special_action.py` による要約・解説・これ何・ファクトチェック処理）。
   - ゲームツール基盤（20doors / うみがめ、お題生成、`GamePoolManager`）。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/tool_registry/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `observation/`, `memory_manager/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **外部通信の包括的例外処理と多重防御**:
   - 外部APIやMCPサーバーとの通信において、接続タイムアウト、切断（ServerDisconnectedError）、不正JSONなどの例外を確実に捕捉し、上位プロセスをクラッシュさせてはならない。
3. **目的語ハイジャック遮断とクエリ検証**:
   - Web調査や特殊アクションにおいて、直前の無関係な雑談から勝手に単語を取り込んで目的外の検索クエリを生成（目的語ハイジャック）してはならない。
4. **ツール実行結果の型安全性**:
   - ツール実行結果（`ToolResult`）は、フェーズ4（Observation）が安全に解釈できるよう、一貫したフォーマット（`output`, `error`, `success`）を維持すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest tests/test_harness_tool_registry.py tests/test_harness_mcp.py tests/test_special_action.py
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/tool_registry
  ```
