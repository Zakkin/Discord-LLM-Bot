# 🔍 Observation & Evaluation (`harness.agent.observation`)

Discord AI Bot における「結果の観察と評価（Observation / Phase 4）」を担当するコアハーネスパッケージです。

---

## 📖 概要 (Overview)

本パッケージは、AIエージェント・ハーネスアーキテクチャの **「フェーズ4. 結果の観察と評価（Observation）」** を具現化するコンポーネントです。

ツール（Action）から返ってきた結果（APIレスポンス、スクリプト実行ログ、検索結果など）をエージェントが「観察」し、
目的達成度を評価して次の推論・計画（フェーズ2: Thought）へ戻すか、最終応答（フェーズ5: Final Response）へ移行するかを判定します。

### 階層ツリー構造における位置づけ

```text
AIエージェント・ハーネス（全体システム）
  1. 実行基盤（エージェント本体）
    1-1. プロンプトマネージャー(フェーズ1)＆パーサー(フェーズ2)  [harness/agent/prompt_parser]
    1-2. オーケストレーター（フロー制御 フェーズ1/2/3/4/5を制御）
    1-3. メモリマネージャー(フェーズ1/4/5に関係)
    1-4. ツールレジストリ(フェーズ1/3に関係)                      [harness/agent/tool_registry]
    1-5. 結果の観察と評価(フェーズ4)                              [★本パッケージ: harness/agent/observation]
       ├── ツール実行結果の精査と正規化 (ObservationProcessor)
       ├── 目的達成度・エラー評価 (ObservationEvaluator)
       ├── エラーループ・同一呼び出し抑止 (LoopDetector)
       └── プロンプト・文脈再合成 (ObservationSynthesizer)
  2. テストハーネス（評価・安全性基盤）                           [harness/agent/test_harness]
```

### 主な役割
1. **観察プロセッサ (`observer.py`)**:
   - `ToolResult`（Action実行結果）を受け取り、エラー・タイムアウト・レートリミット・ログインウォール・空結果等のステータスを識別。
   - 長大テキストのスマートなトランケーションおよびサニタイズ。
   - OpenAI/Ollama互換の `role: "tool"` メッセージ形式へ変換。
2. **観察評価器 (`evaluator.py`)**:
   - ツール実行結果がユーザーの目標を達成しているか（`GOAL_ACHIEVED`）、追加情報が必要か（`NEED_MORE_INFO`）、引数修正して再試行すべきか（`RETRY_WITH_CORRECTION`）、別ツールへ切り替えるべきか（`ALTERNATIVE_TOOL`）を判定。
   - 再思考（フェーズ2: Thought）用のフィードバック指示文を自動生成。
3. **エラーループ検知器 (`loop_detector.py`)**:
   - 同一引数での連続呼び出し、同一エラーの反復、ターン数上限超過を検知し、エージェントの暴走や無限ループを遮断。
4. **コンテキスト合成器 (`synthesizer.py`)**:
   - 過去の複数ツールの実行結果・観察履歴を集約し、LLMコンテキストウィンドウに収まるよう要約。
5. **感情観察評価器 (`emotion_evaluator.py`)**:
   - ユーザー入力に対する感情変化、感情スコアの正規化、および推論理由（reason）の品質観察・評価。
   - AGENTS.md に基づく中国語混入検知、架空事実ハルシネーション遮断、他者漏洩防止を徹底。


---

## 🔄 5段階内部処理フローにおけるフェーズ4 (Observation)

```mermaid
flowchart TD
    P3["フェーズ3: ツール選択と実行 (Action)"] --> P4["★ フェーズ4: 結果の観察と評価 (Observation)"]
    
    subgraph Phase4["フェーズ4: 内部処理"]
        P4A["実行結果の観察 & サニタイズ (ObservationProcessor)"] --> P4B["エラーループ & 安全性検知 (LoopDetector)"]
        P4B --> P4C["目的達成度 & 妥当性評価 (ObservationEvaluator)"]
        P4C --> P4D["コンテキスト合成 (ObservationSynthesizer)"]
    end
    
    P4D -->|GOAL_ACHIEVED (情報十分)| P5["フェーズ5: 最終応答の生成 (Final Response)"]
    P4D -->|RETRY / NEED_INFO (不十分・修正)| P2["フェーズ2: 推論と計画立案 (Thought)"]
```
