# 🛡️ Test Harness (`harness.agent.test_harness`)

AIエージェント・ハーネスアーキテクチャにおける **「2. テストハーネス（評価・安全性基盤）」** を担当する統合基盤パッケージです。

エージェントの監視（テレメトリ）・検証（エバリュエーター）・保護（サンドボックス）を担います。

---

## 📖 概要 (Overview)

本テストハーネスは、自律的にツール選択・実行（Action）や推論ループを回すAIエージェントが、安全かつ期待通りに動作することを保証するための評価・安全性基盤です。

### 階層ツリー構造における位置づけ

```text
AIエージェント・ハーネス（全体システム）
  1. 実行基盤（エージェント本体）
    1-1. プロンプトマネージャー(フェーズ1)＆パーサー(フェーズ2)  [harness/agent/prompt_parser]
    1-2. オーケストレーター（フロー制御 フェーズ1/2/3/4/5を制御）
    1-3. メモリマネージャー(フェーズ1/4/5に関係)
    1-4. ツールレジストリ(フェーズ1/3に関係)                      [harness/agent/tool_registry]
    1-5. 結果の観察と評価(フェーズ4)                              [harness/agent/observation]
  2. テストハーネス（評価・安全性基盤）                           [★本パッケージ: harness/agent/test_harness]
    ├── 2-1. サンドボックス実行環境 (sandbox/)
    │   ├── 隔離された実行空間の提供（Docker, Firecracker, WebAssembly, 権限制限ローカル）
    │   └── ホスト環境を保護するための権限制限（パストラバーサル遮断、危険コマンド拒否）
    ├── 2-2. テレメトリ・オブザーバビリティ (telemetry/)
    │   ├── 思考プロセスやツール呼び出しの分散トレース (DistributedTracer)
    │   ├── トークン消費量とレイテンシの計測 (MetricsCollector)
    │   └── ログとデバッグ情報の永続化 (TelemetryStorage / JSONL)
    └── 2-3. エバリュエーター (evaluator/)
        ├── タスク達成度の機械的チェック（アサーション / MechanicalAssertion）
        ├── LLM-as-a-Judge（別モデルによる定性評価・採点 / LLMAsAJudge）
        └── 環境状態の検証（指定ファイル作成・差分検証 / EnvironmentVerifier）
```

---

## 🧩 サブコンポーネント詳細

### 2-1. サンドボックス実行環境 (`sandbox/`)
- **`LocalRestrictedSandbox`**: ホスト環境を保護するための権限制限付きローカル隔離空間。パストラバーサル（`../`）や危険コマンド（`rm -rf /`, `sudo`, `shutdown` 等）を事前遮断し、サブプロセスのタイムアウト・メモリ・出力上限を制御。
- **`DockerSandbox`**: Dockerコンテナによるネットワーク完全遮断（`--network none`）、読み取り専用マウント、リソース制限（CPU/Memory）を提供。
- **`WasmSandbox`**: WebAssemblyランタイム（WasmEdge/Wasmtime等）による軽量分離実行環境の抽象化。

### 2-2. テレメトリ・オブザーバビリティ (`telemetry/`)
- **`DistributedTracer`**: フェーズ1（Context）〜 フェーズ5（Final Response）に至る一連のエージェント推論・ツール呼び出し・観察プロセスを階層スパン（`TraceSpan`）として分散追跡。
- **`MetricsCollector`**: 各フェーズごとのトークン消費量（Prompt/Completion/Total）およびレイテンシ（処理時間秒）を計測・集計。
- **`TelemetryStorage`**: トレースログおよびメトリクス要約を JSONL 形式で永続化保存し、特定トレースIDでの復元・事後検証をサポート。

### 2-3. エバリュエーター (`evaluator/`)
- **`MechanicalAssertion`**: 正規表現マッチ、必須/禁止文字列、JSONスキーマ、思考タグ（`<think>`）漏洩防止、最小文字数を決定論的・機械的にチェック。
- **`LLMAsAJudge`**: 別モデル（または評価専用プロンプト）により、タスク達成度・正確性・キャラクター性/口調・安全性のルーブリックに基づき 1.0〜5.0 点で定性評価。
- **`EnvironmentVerifier`**: エージェントのアクションによって指定ファイルが作られたか、ファイル内容の整合性、ディレクトリのスナップショットDiff検証を実施。
