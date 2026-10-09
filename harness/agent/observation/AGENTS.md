# 🔍 Observation & Evaluation ハーネス開発・運用ガイドライン (AGENTS.md)

本ドキュメントは、AIエージェント・ハーネスアーキテクチャにおける **「フェーズ4. 結果の観察と評価（Observation & Evaluation）」** を担う `harness/agent/observation` ディレクトリの設計前提、アーキテクチャ仕様、コンポーネント責務、ガードレール、および禁止事項（Ban List）を定義する正本ガイドラインです。

---

## 🧭 1. 概要とハーネス全体における位置づけ

本パッケージは、AIエージェントが実行したツール（フェーズ3: Action）や外部環境からのフィードバックを客観的に「観察（Observe）」し、その妥当性と目的達成度を「評価（Evaluate）」した上で、次のフェーズ（フェーズ2: Thought への再思考・巻き戻し、またはフェーズ5: Final Response への進行）へと安全に状態遷移（Route）させる責務を持ちます。

### 5段階内部処理フローにおけるフェーズ4の位置づけ

```text
AIエージェント・ハーネス（5つの実行フェーズ）
  ├─ フェーズ1: 意図理解とコンテキスト準備 (Prompt Management / Context Preparation)
  ├─ フェーズ2: 推論と計画立案 (Thought / Reasoning / Planning)
  ├─ フェーズ3: ツール選択と実行 (Action / Tool Execution)
  ├─ ★ フェーズ4: 結果の観察と評価 (Observation & Evaluation)  [本ディレクトリ]
  └─ フェーズ5: 最終応答の生成 (Final Response Generation)
```

```mermaid
flowchart TD
    P3["フェーズ3: ツール選択と実行 (Action)"] --> P4["★ フェーズ4: 結果の観察と評価 (Observation)"]
    
    subgraph Phase4["フェーズ4: 内部処理フロー"]
        P4_Parse["1. 実行結果のパース & 正規化\n(ObservationParser)"]
        P4_Mem["2. 短期記憶・履歴追記\n(ScratchpadManager)"]
        P4_Loop["3. ループ & 安全ガード検知\n(LoopDetector)"]
        P4_Eval["4. 目的達成度 & 妥当性評価\n(ObservationEvaluator / Domain Evaluators)"]
        P4_Route["5. 次フェーズ状態遷移ルーティング\n(ObservationRouter)"]
        P4_Synth["6. プロンプト用コンテキスト合成\n(ObservationSynthesizer)"]

        P4_Parse --> P4_Mem --> P4_Loop --> P4_Eval --> P4_Route --> P4_Synth
    end
    
    P4_Route -->|PROCEED_TO_FINAL\n(目的達成 / 最大ターン到達)| P5["フェーズ5: 最終応答の生成\n(Final Response)"]
    P4_Route -->|RETRY_WITH_THOUGHT\n(情報不足 / クエリ修正再試行)| P2["フェーズ2: 推論と計画立案\n(Thought)"]
    P4_Route -->|FATAL_ERROR\n(修復不能エラー / 安全停止)| P5_Err["フェーズ5: フォールバック・お詫び応答"]
```

---

## 🏛️ 2. アーキテクチャとコンポーネント構成

本ディレクトリは **Facade パターン** を採用しており、オーケストレーターは原則として `ObservationFacade`（旧エイリアス `ObservationManager`）のみと対話します。内部は責務ごとに疎結合なモジュールへ分離されています。

### ディレクトリ構成と役割一覧

| ファイル | 主要クラス / 関数 | 責務と役割 |
| :--- | :--- | :--- |
| `types.py` | `Observation`, `ObservationStatus`, `EvaluationVerdict`, `NextStepPhase`, `EvaluationResult`, `LoopDetectionResult`, `ScratchpadEntry`, `ObservationRoutingResult` | フェーズ4全体で利用されるデータ構造・列挙型の定義 |
| `facade.py` | `ObservationFacade` (`ObservationManager`) | フェーズ4全体のオーケストレーションを一元管理するエントリーポイント |
| `parser.py` | `ObservationParser` (`ObservationProcessor`) | ツール実行結果（MCP、ToolResult、Exception等）の解体・正規化・ステータス分類 |
| `observer.py` | 後方互換エクスポート | `parser.py` への透過的エイリアスモジュール |
| `memory.py` | `ScratchpadManager` (`ObservationMemory`) | ターン別短期記憶（スクラッチパッド）管理、対話履歴（`messages`）更新、履歴圧縮 |
| `loop_detector.py` | `LoopDetector` | 同一ツール・同一引数ループ検知、連続エラー遮断、最大ターン数超過監視 |
| `evaluator.py` | `ObservationEvaluator` | 観察結果のステータス・内容評価、再思考プロンプト（`rethink_prompt`）生成 |
| `router.py` | `ObservationRouter` | 評価結果を集約し、次フェーズ（`NextStepPhase`）への遷移を安全に決定 |
| `synthesizer.py` | `ObservationSynthesizer` | フェーズ2用フィードバックおよびフェーズ5用確定事実（Verified Facts）のプロンプト合成 |
| `web_evaluator.py` | `WebObservationEvaluator` | Web検索の目的語ハイジャック検知、幻覚単語除去、ノイズドメイン（歌詞サイト等）除外 |
| `emotion_evaluator.py` | `EmotionObservationEvaluator`, `EmotionEvaluation` | 感情スコア・理由の観察評価、中国語・簡体字混入遮断、架空事実ハルシネーション遮断 |
| `fact_evaluator.py` | `FactCheckEvaluator`, `FactCheckVerdict`, `FactCheckEvaluation` | ファクトチェックツールの結果観察、真偽判定（True/False/Unverifiable等）と信頼度算出 |
| `reply_evaluator.py` | `ReplyObservationEvaluator`, `ReplyObservationResult` | LLM生成応答の観察、不知スキップ、オウム返し検知、思考タグ漏洩・途切れ検知 |
| `feedback_observer.py` | `FeedbackObserver`, `FeedbackObservation` | Discordユーザーのリアクション絵文字・テキスト指摘の観察と感情デルタ算出 |
| `reflection_evaluator.py` | `ReflectionEvaluator`, `ReflectionEvaluation` | 定期内省ログの観察、第三者呼称誤認防止、罵倒受容信念の遮断、単発エビデンス排除 |

---

## 📊 3. コアデータモデル (`types.py`)

### 1. `ObservationStatus` (実行ステータス)
ツールの生の結果を以下の標準ステータスに正規化します：
- `SUCCESS`: 正常終了（有意な結果あり）
- `EMPTY_RESULT`: 正常終了だが該当結果なし
- `PARTIAL_SUCCESS`: 一部成功（追加情報が必要）
- `ERROR`: ツール実行エラー
- `TIMEOUT`: タイムアウト（504, 408等）
- `PERMISSION_DENIED`: 権限制限・サンドボックス拒否（403等）
- `RATE_LIMITED`: APIレート制限（429等）
- `LOGIN_WALL`: ログインウォール・スクレイピング遮断
- `PROTOCOL_ERROR`: MCP / JSON-RPC 通信プロトコルエラー
- `SERVER_ERROR`: 外部HTTP 500/502/503障害
- `BAD_ARGUMENTS`: 引数バリデーションエラー（400等）
- `UNKNOWN`: 未知のステータス

### 2. `EvaluationVerdict` (評価判定)
- `GOAL_ACHIEVED`: 目的達成（フェーズ5 Final Response へ進行可能）
- `NEED_MORE_INFO`: 情報不足（フェーズ2 Thought へ戻り追加情報収集）
- `RETRY_WITH_CORRECTION`: 引数・クエリ修正して再試行（フェーズ2/3へ戻る）
- `ALTERNATIVE_TOOL`: 別のツール・別アプローチへ切り替え（フェーズ2/3へ戻る）
- `FATAL_ERROR`: 修復不能なエラー・安全停止（フォールバック応答へ）

### 3. `NextStepPhase` (次フェーズ遷移先)
- `PROCEED_TO_FINAL`: フェーズ5（最終応答生成）へ進行
- `RETRY_WITH_THOUGHT`: フェーズ2（推論・計画立案）へ巻き戻し
- `FATAL_ERROR`: 安全停止（エラー説明・お詫び応答）

---

## 🛡️ 4. ガードレールと重要不具合再発防止仕様

本パッケージには、プロジェクト全体規約（`AGENTS.md` / `ban_list.md`）に定められた重要なバグ再発防止ガードが実装されています。

### ① ファクトチェック・Web調査の目的語ハイジャック遮断 (`web_evaluator.py`)
- **対応項目**: AGENTS.md 項目75
- **仕様**:
  - 対象文が自立している場合、直前雑談（「王たん」「新曲」等）の目的語をクエリに混入させない（`is_query_hijacked`）。
  - クエリ内に文脈・対象文に存在しない外部単語が含まれる場合、自動除去・サニタイズ（`sanitize_query_hallucinations`）。
  - 歌詞サイト（uta-net.com, utaten.com 等）や空結果は自動フィルタリング（`filter_clean_results`）。

### ② 理解不能・不知返答の無理な送信抑止（スキップ機能） (`reply_evaluator.py`)
- **対応項目**: AGENTS.md 項目77
- **仕様**:
  - 一般サブチャンネルでの雑談において「〜についてよくわからない」「意味がつかめない」等の不知・困惑が出力された場合、無理に返信せず送信をスキップ（`should_skip_unknown_reply` -> `should_skip_sending = True`）。
  - **例外保護**: メインチャンネル（Primaryチャンネル）での発言、または直接メンション・返信（`force_response=True`）された場合はスキップせず、必ず応答を返却。

### ③ 小型推論モデルの思考暴走・タグ漏洩・オウム返し防止 (`reply_evaluator.py`)
- **対応項目**: AGENTS.md 項目15, 20, 23, 44, 47, 86
- **仕様**:
  - `<think>` タグ漏洩、英語検証メモ、プロンプト指示漏洩を検知して再試行を要求（`looks_like_reasoning_leak`, `looks_like_prompt_leak`）。
  - 相手発言の丸写し（一致度0.85以上）をオウム返しとして検知し、差し戻し（`looks_like_parrot_reply`）。
  - トークン枯渇等による文末途切れ（`looks_like_truncated_reply`）を検知して完全な文章での再生成を要求。

### ④ 感情観察における中国語混入検知と事実捏造遮断 (`emotion_evaluator.py`)
- **対応項目**: AGENTS.md 項目23, 24, 87, 90
- **仕様**:
  - 小型モデル特有の中国語・簡体字（「会继续」「继续」「觉得」等）の混入を即時検知（`contains_cjk_non_japanese`）。混入時は安全な定型文（`fallback_reason_text`）へ自動フォールバック。
  - 述語（「〜したためです」等）の欠落した断片テキストを排除（`looks_like_bad_reason_text`）。
  - 感情理由テキスト内のセリフ調テキストや他者言及をサニタイズし、長期記憶への事実捏造混入を水際で遮断。

### ⑤ 定期内省における第三者名呼称誤認防止 (`reflection_evaluator.py`)
- **対応項目**: AGENTS.md 項目30, 93
- **仕様**:
  - 内省信念（Belief）内で「相手を『〇〇』と呼ぶ」ルールが生成された際、抽出された呼称が対象ユーザー名と乖離している場合は誤認として却下。
  - 呼称ルールおよび信念形成において、単発エビデンス（`evidence_count < 2`）は棄却。
  - 暴言・罵倒を受容・肯定する不健全な信念を確実に遮断。

### ⑥ エラーループ・同一呼び出しの完全遮断 (`loop_detector.py`)
- **対応項目**: AGENTS.md 項目40, 78
- **仕様**:
  - ツール名と引数のMD5ハッシュで呼び出し履歴を管理。
  - 同一引数での連続呼び出しが `max_identical_calls`（デフォルト2回）に達した時点で `ALTERNATIVE_TOOL`（別アプローチ指示）を発行。
  - 連続エラーが `max_consecutive_errors`（デフォルト3回）に達した時点で警告。
  - セッション総ターン数が `max_total_turns`（デフォルト10回）に達した時点で強制的に `abort_execution = True`（安全停止）を発行。

---

## 🚫 5. 禁止事項 (Ban List)

本パッケージ（`harness/agent/observation`）の開発・修正にあたっては、以下の事項を厳格に禁止します：

1. **特定キャラクター・特定ボット設定のハードコード禁止**:
   - `web_evaluator.py`, `emotion_evaluator.py` などの共通コード内に、特定のキャラクター名（コイト、シャンクス等）や特定チャンネルID、特定の口癖をハードコードしてはならない。
2. **生例外の握りつぶし・未分類放置の禁止**:
   - ツール実行エラーや通信例外が発生した際、単に `status="error"` で済ませず、必ず `TIMEOUT`, `RATE_LIMITED`, `PERMISSION_DENIED`, `LOGIN_WALL`, `PROTOCOL_ERROR`, `SERVER_ERROR`, `BAD_ARGUMENTS` などの詳細ステータスへ分類すること。
3. **無限ループ・再試行の無制限化禁止**:
   - `LoopDetector` をバイパスして再試行を無限に繰り返してはならない。最大ターン数および同一引数チェックを必ず経由させること。
4. **長大テキストの未サニタイズ・未トリミング注入禁止**:
   - 数万文字におよぶツールの生出力をそのまま対話履歴（`messages`）やプロンプトに注入してはならない。`ScratchpadManager` および `synthesizer.py` の文字数制限（`max_message_chars`, `max_total_chars`）を遵守すること。
5. **破壊的履歴操作の禁止**:
   - スクラッチパッドや対話履歴をトリミングする際、システムプロンプトやユーザーの最初の入力プロンプトを消失させてはならない（`keep_initial_system=True` の保護原則）。
6. **未検証の感情理由・推論メモの長期記憶・プロンプト漏洩禁止**:
   - 壊れた感情理由テキスト（簡体字混入、述語なし、主観的愚痴）をそのままフェーズ5や長期記憶抽出パイプラインへ渡してはならない。必ず `EmotionObservationEvaluator` で検証・正規化すること。
7. **型アノテーションの省略・`mypy` エラー放置の禁止**:
   - Python 3.11+ 型ヒント（`from __future__ import annotations`）を徹底し、戻り値型・引数型を完全に明記すること。`Any` の多用を避け、`types.py` のモデルを利用すること。

---

## 🧪 6. 開発・検証・保守手順

### 1. 静的型チェック (`mypy`)
変更を加えた際は、型エラーが 0 件であることを必ず確認してください：
```bash
mypy harness/agent/observation
```

### 2. 単体テスト実行
本ディレクトリの各種評価器・パーサー・ルーターの挙動を検証します：
```bash
pytest harness/agent/observation/tests
```

### 3. コンポーネント追加時のチェックリスト
新しいドメイン評価器やパーサー機能を追加する場合：
- [ ] `types.py` に必要なステータスや判定モデルが定義されているか
- [ ] `ObservationFacade` に適切な統合メソッドが配備されているか
- [ ] `LoopDetector` による安全ガードが機能しているか
- [ ] AGENTS.md ガードレール（サニタイズ、中国語検知、目的語ハイジャック遮断等）に違反していないか
- [ ] `__init__.py` の `__all__` に公開クラス・関数が登録されているか
- [ ] `mypy` 検査がパスし、エラーが 0 件であるか
