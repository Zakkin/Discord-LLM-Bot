# 🤖 Observation ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/observation/`）は、AIエージェントの**フェーズ4（Observation/結果の観察と評価）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/observation/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/observation/` 外）のファイルを直接編集してはならない。
> 2. **ループ検知と反復制御**: 同一ツール・同一引数（2回）に加え、引数が異なる同一ツール連続呼び出し（`max_consecutive_same_tool` 5回）や連続エラー（3回）を確実に遮断し、推論コンテキスト肥大化を防ぐこと。
> 3. **評価器多重防御の維持**: 感情スコアリング統括（`evaluate_emotion_scoring`）、不知スキップ（`should_skip_sending`）、テキスト指摘観察（`observe_text_feedback`）、クエリサニタイズ・タイポ補正（`sanitize_query_hallucinations`）、および感情理由のロール反転受身検知拡充（`has_role_reversal_in_other_user_reply`）・三人称客観AI主語サニタイズ（`looks_like_bad_reason_text`、Bot名パターンの動的Config化）の整合性を維持すること。
> 4. **情報充足度（Information Gap）評価および対話相手名誤爆防止・フォールバック型安全性の維持**: 検索結果の表層的一致（概要文のみ）を遮断し、主要語被覆率（比較片落ち検知）および時期・理由・価格スペックのギャップ検知と過剰リトライ防止安全フォールバック（`current_turn >= 3`）を維持すること。対話相手名と同名の別キャラクター誤爆時は混同防止注意書きを付与しノイズとして棄却すること。また `detect_search_fallback` では `isinstance(data, dict)` 型ガードを死守し、非dictデータ渡来時の `AttributeError` を防止すること。
> 5. **Deep Reflection 主語評価の救済**: `evaluate_deep_reflection_result` において `target_user_name` の受け取りと「ユーザー」主語の特定ニックネームまたは「対話相手」への自動正規化（`normalize_deep_reflection_policy`）により、全体行動指針が誤棄却されることを防ぐこと。
> 6. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。

