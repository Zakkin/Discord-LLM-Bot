# 🤖 Observation サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/observation/`）は、AIエージェントの**フェーズ4（Observation/結果の観察と評価）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **結果観察と評価パイプライン**:
   - `ObservationProcessor`, `ObservationEvaluator`, `ObservationRouter` によるツール実行結果の解釈と評価。
   - 目的達成度（`EvaluationVerdict`: `SUCCESS`, `PARTIAL`, `FAILURE`, `LOOP_DETECTED`）の判定。
2. **エラーループ検知と制御**:
   - `LoopDetector` による無限ループ（同一ツール連続呼び出し上限2回、連続エラー上限3回、最大ターン数制御）の検知と遮断。
   - 再推論へのフォールバック（`NextStepPhase.REFLECT`）または最終回答への強制移行（`NextStepPhase.FINAL_RESPONSE`）の決定。
3. **推論用合成とフィードバック**:
   - `ObservationSynthesizer` による次ターン推論用テキスト（Observationブロック）の簡潔な統合・フォーマット。
   - `FeedbackObserver` によるユーザーからのフィードバック（訂正・ツッコミ・リアクション）の収集・評価。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/observation/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `tool_registry/`, `memory_manager/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **トークン枯渇の防止**:
   - ツール実行結果の生データ（大量のHTMLやログ等）をそのまま推論コンテキストに流し込まず、`ObservationProcessor` で要約・トランケートしてコンテキスト肥大化を防ぐこと。
3. **ループ検出の確実性**:
   - エラーが発生し続けた場合に、永久にツール再試行を繰り返してプロセスをハングさせてはならない。規定回数で確実に `LOOP_DETECTED` を判定すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest harness/agent/observation/tests/
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/observation
  ```
