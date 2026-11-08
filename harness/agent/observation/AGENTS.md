# 🤖 Observation サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/observation/`）は、AIエージェントの**フェーズ4（Observation/結果の観察と評価）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **結果観察と評価パイプライン**:
   - `ObservationProcessor`, `ObservationEvaluator`, `ObservationRouter` によるツール実行結果の解釈と評価。
   - 目的達成度（`EvaluationVerdict`: `GOAL_ACHIEVED`, `NEED_MORE_INFO`, `RETRY_WITH_CORRECTION`, `ALTERNATIVE_TOOL`, `FATAL_ERROR`）の判定。
   - **検索結果の充足度評価（Information Gap検知）および対話相手名衝突ガード**:
     - `ObservationEvaluator.evaluate()` / `detect_information_gap()` による表層的一致（概要文の存在のみ）の誤通過遮断。
     - **対話相手名誤爆（同名キャラ混同）検知・注意書き付与**:
       - `tool_name == "search_web"` において、検索結果に対話相手名（`target_name`）と同名の架空キャラクター・別人物の辞書記事（ピクシブ百科事典、Wikipedia、CV、アルコバレーノ等）が含まれ、ユーザーがその作品について尋ねていない場合の誤爆検知（`detect_target_name_character_conflict`）。
       - 混同防止注意書き（`※注意: 検索結果の『{target_name}』は同名の別キャラクターの情報です。目の前の対話相手『{target_name}』さん本人の属性として混同しないでください`）の付与およびノイズ棄却（`RETRY_WITH_CORRECTION`）。
       - 評価完了時の詳細ログ出力（`[evaluator] Tool=%s goal_satisfied=%s should_continue=%s fallback_detected=%s output_len=%d`）。
     - **主要語被覆率（Coverage）チェック**: 助詞・ストップワードを除外した2文字以上の実質語抽出（`extract_key_terms`）、比較クエリ（「AとBの比較」等）の片落ち検知、複数主要語の全欠落検知。
     - **疑問意図（Question Intent）と解決状況の照合**:
       - 時期・時間系（WHEN: 発売日・日時・年号）の充足度判定
       - 原因・理由系（WHY / HOW: 原因・理由・仕組み・手順）の充足度判定
       - 数値・スペック・価格系（SPECS / HOW MUCH: 価格・金額・スペック数値）の充足度判定
     - **過剰リトライ防止**: `current_turn >= 3` における安全フォールバック（無限ループ防止）。
     - **検索フォールバック検知における型ガード保護**:
       - `detect_search_fallback` において、`Observation.data` が `dict` の場合のみ `.get()` を呼び出す型ガード（`isinstance(observation.data, dict)`）を徹底し、文字列（`str`）やリスト（`list`）等が渡された際の `AttributeError: 'str' object has no attribute 'get'` によるクラッシュ・フォールバック二重推論（ステール返信原因）を完全遮断。
2. **エラーループ検知と制御**:
   - `LoopDetector` による無限ループ・徘徊の検知と遮断:
     - 同一ツール・同一引数の反復実行制限（`max_identical_calls`: デフォルト2回）
     - 同一ツールの連続実行制限（引数が異なる反復呼び出しも含む `max_consecutive_same_tool`: デフォルト5回で警告、超過で強制中断）
     - 連続エラー上限（`max_consecutive_errors`: デフォルト3回）
     - 最大総ターン数制御（`max_total_turns`: デフォルト10ターン）
   - 再推論へのフォールバック（`NextStepPhase.REFLECT`）または最終回答への強制移行（`NextStepPhase.FINAL_RESPONSE`）の決定。
3. **各種ドメイン評価器（ドメイン別Observation）**:
   - `EmotionObservationEvaluator`:
     - 感情スコアリング・出来事評価統括（`evaluate_emotion_scoring` -> `EmotionScoringEvaluation`）
     - 出来事評価テキストの健全性検査（`looks_like_bad_appraisal_text`、簡体字混入遮断、フォールバック）
     - 有意シグナル判定（`has_meaningful_emotion_signal`）および言葉遊び・自己状態質問の被害妄想緩和（`mitigate_wordplay_paranoid_anger`）
     - 感情理由・出来事評価の一人称汚染ガードと正規化（`normalize_reason_text`, `looks_like_bad_reason_text`）
     - 他者宛て返信におけるロール反転受身検知（`has_role_reversal_in_other_user_reply`）: 「言われ」「怒られ」「注意され」等に加え、「聞かれ」「尋ねられ」「問われ」「質問され」「問い詰められ」「詰められ」「迫られ」「要求され」「求められ」「話しかけられ」「振られ」を検知し自己当事者化を遮断。
     - 三人称・客観AI主語サニタイズ（`looks_like_bad_reason_text`）: 文頭の `^(?:AI|AIアシスタント|bot|ボット|{names_pattern})\s*(?:が|は|にとって)` や `^(?:AI\s+[ぁ-んァ-ヶー一-龠a-zA-Z0-9_]+)\s*(?:が|は)`（例: `AI アシスタントが`）を不良理由として検知しフォールバック理由へ置換。
     - 動的Bot名パターン構成（`_get_bot_name_patterns`）: 特定キャラ名をハードコードせず、普遍的一人称・AI一般呼称（`私`, `自分`, `AI`, `bot` 等）を初期値とし、`lib.config_utils.cfg`（`OLLAMA_BOT_DISPLAY_NAME`, `BOT_FIRST_PERSON`, `BOT_ALIASES`）から動的に主語ガード対象のBot名リストを構成。
     - 行動方針導出（`evaluate_action_policy`）
   - `ReplyObservationEvaluator`:
     - 生成応答の品質評価（オウム返し、思考漏洩、文末途切れ等の検知）
     - 不知・理解不能スキップ（`should_skip_unknown_reply` -> `should_skip_sending=True`）の判定
   - `FeedbackObserver`:
     - リアクション観察（`observe_reaction`）およびテキスト指摘観察（`observe_text_feedback` -> `FeedbackObservation`）
     - 不自然な日本語、事実誤認、スタイル逸脱、オウム返し指摘の分類とセンチメント反映
   - `WebObservationEvaluator`:
     - Web検索・ブラウジング結果の観察・妥当性評価（ノイズドメイン除外・クエリハイジャック検知・情報十分性評価）
     - 検索クエリサニタイズ（`sanitize_query_hallucinations`）:
       - カタカナ語・固有名詞の判定厳格化（3文字以上のカタカナ語・英数字の2文字部分一致素通り廃止、完全一致・部分文字列優先）
       - タイポ補正（対象原文への引き戻し機構: `_correct_katakana_typo` による編集距離1のカタカナ語正規化）
       - 検索ノイズスラング・罵倒表現の安全除外（`_NOISE_SLANG_WORDS`: 「ガキ」「クソ」等）
       - 結合助詞（「の」「と」）の自動分離（`_split_particles`）によるトークン単位サニタイズ
   - `FactCheckEvaluator`, `ReflectionEvaluator` によるファクトチェック・内省結果の観察:
     - `evaluate_deep_reflection_result`: 深層内省（Deep Reflection）における `target_user_name` 受け取りと「ユーザー」主語の特定ニックネーム（『{target_user_name}』さん）または「対話相手」への自動正規化（`normalize_deep_reflection_policy`）による全体行動指針の誤棄却救済。
     - `validate_reflection_belief`: `allow_generic_subject` による全体内省方針（「対話相手が〜」等）の許容。
4. **推論用合成とフィードバック**:
   - `ObservationSynthesizer` による次ターン推論用テキスト（Observationブロック）の簡潔な統合・フォーマット。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/observation/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `tool_registry/`, `memory_manager/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **トークン枯渇の防止**:
   - ツール実行結果の生データ（大量のHTMLやログ等）をそのまま推論コンテキストに流し込まず、`ObservationProcessor` で要約・トランケートしてコンテキスト肥大化を防ぐこと。
3. **ループ検出の確実性と反復制御**:
   - エラーが発生し続けた場合に、永久にツール再試行を繰り返してプロセスをハングさせてはならない（連続3回で遮断）。
   - 引数が微妙に異なる同一ツールの連続呼び出しについても、`max_consecutive_same_tool` により確実に検知し、徘徊ループを遮断すること。
4. **感情評価・出来事評価の多重防御の維持**:
   - `evaluate_emotion_scoring` をバイパスして生テキストや壊れたappraisalをそのまま後続処理やプロンプトへ流し込んではならない。
   - 簡体字・中国語の混入、被害妄想（言葉遊びに対する怒り等）は必ずObservationレイヤーで検知・緩和すること。
5. **応答スキップ判定の整合性保護**:
   - 不知・理解不能返信の送出スキップ（`ReplyObservationResult.should_skip_sending`）および評価ステータス（`EvaluationVerdict.GOAL_ACHIEVED`）のアサーション整合性を維持すること。
6. **検索クエリサニタイズ・タイポ補正の整合性維持**:
   - `sanitize_query_hallucinations` において、安易な文字単位オーバーラップ（2文字一致等）で幻覚・誤字単語を素通りさせてはならない。
   - 対象文に実在するカタカナ語への引き戻し（編集距離1）および検索ノイズスラング（「ガキ」等）の除外、助詞「の」「と」の分離による多重防御を維持すること。
7. **情報充足度（Information Gap）評価の整合性維持**:
   - 検索結果に概要文（「〇〇とは」等）が含まれているだけで安易に `GOAL_ACHIEVED` と判定してはならない。
   - ユーザー質問の核心（発売日・原因・価格等）のギャップおよび比較クエリの片落ちを確実に検知し、`RETRY_WITH_CORRECTION` を返却すること。
   - リトライプロンプト（`rethink_prompt`）は項目48に基づき1〜2文の具体的かつ簡潔な指示にとどめること。
   - 項目111（ゼロLLMオーバーヘッド）を遵守し、外部LLM呼び出しを行わずPython標準の正規表現・文字列処理のみで高速に判定すること。
   - 無限リトライ防止のため、`current_turn >= 3` における安全フォールバック（現状情報でのフェーズ5進行）を維持すること。
   - **`detect_search_fallback` における型ガードの徹底**: `Observation.data` は `Any` 型でありうるため、`if observation.data:` で直接 `.get(...)` を呼ばず、必ず `if isinstance(observation.data, dict):` で型ガードを行うこと。非dictデータ（`str` 等）渡来時の `AttributeError` による二重推論クラッシュを防止すること。
8. **自己状態・関係性質問への被害妄想怒り緩和と一人称汚染ガード・三人称客観主語排除・ロール反転検知（項目#26, #118, #156, #157）**:
   - ユーザーからBotの感情・態度・関係性についての質問（「俺はなんで恐れられてるの？」「なんで恐れてるの？」「何怒ってんだよ」等）を受けた際、Bot側が「相手が説明を無視して執拗に問い詰めてきた」等の被害妄想で `anger` / `disgust` / `BAD` を暴走させる現象を `mitigate_wordplay_paranoid_anger` で検知・緩和（スコア0.15以下、BAD->NONE、appraisal補正）すること。
   - `looks_like_bad_reason_text` および `looks_like_bad_appraisal_text` において、`BOT_FIRST_PERSON` と異なる一人称（`俺の`, `僕が`, `ワイは` 等）や被害妄想パターン（`説明を無視され`, `執拗に問い詰め`, `被害者面` 等）に加え、三人称・客観AI主語パターン（`^(?:AI|AIアシスタント|bot|ボット|{names_pattern})\s*(?:が|は|にとって)`、`^(?:AI\s+[ぁ-んァ-ヶー一-龠a-zA-Z0-9_]+)\s*(?:が|は)`）を確実に不正判定（フォールバック）すること。主語ガード対象のBot名パターンは特定キャラ名をハードコードせず、`lib.config_utils.cfg`（`OLLAMA_BOT_DISPLAY_NAME`, `BOT_FIRST_PERSON`, `BOT_ALIASES`）から動的に取得すること。
   - 他者宛て返信において、`has_role_reversal_in_other_user_reply` により「言われ」「怒られ」「注意され」等に加え「聞かれ」「尋ねられ」「問われ」「質問され」「問い詰められ」「詰められ」「迫られ」「要求され」「求められ」「話しかけられ」「振られ」を含む自己当事者化・ロール反転受身パターンを確実に検知・遮断すること。
   - `normalize_reason_text` において、引用カギ括弧外の誤った一人称（`俺`, `僕`, `ワイ` 等）を設定一人称（未設定時は `私`）へ自動置換し、自己引用メタ説明を客観的表現へ正規化すること。
9. **対話相手名と同名の架空キャラクター混同遮断とDeep Reflection主語評価（項目#92, #114）**:
   - `search_web` 結果において、対話相手名（`target_name`）と同名のアニメ・ゲームキャラ等の辞書記事がヒットし、かつユーザーがその作品について尋ねていない場合は、目の前の対話相手の属性として混同することを防ぐ注意書き（`※注意: 検索結果の『{target_name}』は同名の別キャラクターの情報です。目の前の対話相手『{target_name}』さん本人の属性として混同しないでください`）を必ず付与し、ノイズとして棄却（`RETRY_WITH_CORRECTION`）すること。
   - 評価完了時に `[evaluator] Tool=%s goal_satisfied=%s should_continue=%s fallback_detected=%s output_len=%d` の詳細ログを出力すること。
   - `evaluate_deep_reflection_result` において、`target_user_name` を欠落させたまま常に空文字で検証に回し、抽象主語判定で全体行動指針を100%リジェクトしてはならない。`normalize_deep_reflection_policy` により「ユーザー」主語を「『{target_user_name}』さん」または「対話相手」へ自動正規化して救済すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
  - `Observation | None` などの Optional 属性にアクセスする際は、テストコード内でも `assert obj is not None` 等で明示的に型を絞り込むこと。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest harness/agent/observation/tests/
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/observation
  ```
