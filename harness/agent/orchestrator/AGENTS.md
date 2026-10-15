# 🤖 Orchestrator サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/orchestrator/`）は、AIエージェントの**オーケストレーション（5段階ループ統括・自発発話・自律制御）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **5段階推論・行動ループ制御**:
   - `AgentOrchestrator`（`engine.py`）による Context ➜ Thought ➜ Action ➜ Observation ➜ Final Response のステートマシン制御。
   - `AgentState`, `OrchestratorConfig`, `OrchestratorResult` による実行状態管理。
2. **自発発話・タイムライン巡回**:
   - `proactive.py` による自発的トピック投稿、Xタイムライン巡回、ふたば勢いスレッド取得、ユーザー関心トピックの自発展開。
   - サブチャンネル自発返信確率（サイコロ方式）の制御。
3. **自律ループ・定期内省・モノローグ**:
   - `loops.py` によるバックグラウンド自律タスクの制御。
   - `reflection.py` による定期内省（Deep Reflection）、ユーザー文化・信念の抽出・更新。
   - `monologue.py` による自律思考ループ。
4. **LLMクライアント抽象化**:
   - `llm_client.py` による Ollama API 呼び出しの抽象化と再試行制御。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/orchestrator/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `tool_registry/`, `observation/`, `memory_manager/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **サブチャンネル自発返信確率（サイコロ方式）の保全**:
   - メッセージ1通ごとに独立確率抽選（サイコロ方式）を行う設計を死守すること。
   - 自発返信エンキュー時の即時カウントリセットと同一チャンネル重複エンキュー抑止（`_active_normal_reply_cids`）の多重防御を維持すること。
3. **定期内省におけるコルーチン安全終了と呼称誤認防止**:
   - 先行内省タスク実行中の重複呼び出し時にコルーチンを明示的に `coro.close()` して破棄し、Pythonの `RuntimeWarning: coroutine was never awaited` を絶対に発生させないこと。
   - 対話相手以外の第三者名を相手の呼称として誤認学習しないよう、表示名プロンプト注入と保存前バリデーションを維持すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest harness/agent/orchestrator/tests/ tests/test_reflection_prompt.py
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/orchestrator
  ```
