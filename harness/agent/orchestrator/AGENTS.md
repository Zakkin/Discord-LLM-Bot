# 🤖 Orchestrator サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/orchestrator/`）は、AIエージェントの**オーケストレーション（5段階ループ統括・自発発話・自律制御）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **5段階推論・行動ループ制御**:
   - `AgentOrchestrator`（`engine.py`）による Context ➜ Thought ➜ Action ➜ Observation ➜ Final Response のステートマシン制御。
   - `AgentState`, `OrchestratorConfig`, `OrchestratorResult` による実行状態管理。
   - コンテキスト構築（`_build_context` / `_prepare_context`）における動的 `num_ctx` 拡張（`_resolve_dynamic_generation_limits`）と連動したプロンプト予算計算、コアシステムプロンプト（人格・絶対厳守ルール）の完全保護、および予算超過時のツールカタログ保護と `user_prompt` 優先安全トリミング（`<current_reply_target>` ブロックの保護と削減ブロックログ明記）。
   - 発話意図判定に基づく不要なWeb検索のゲーティング（短文相槌時の `【調査思考ポリシー】` 抑制、対話相手名クエリの遮断）。
   - 最終応答後のメモリ保存（`_save_memory` / `_prune_and_save_context`）におけるBot発話記録（`add_bot_utterance`）および全フロント共通行動台帳（`store.add_bot_activity`）への記録（返信 `reply` / 投稿 `post`）。
2. **自発発話・タイムライン巡回**:
   - `proactive.py` による自発的トピック投稿、Xタイムライン巡回、ふたば勢いスレッド取得、ユーザー関心トピックの自発展開。
   - 静かな時間帯（生活リズム、`BOT_QUIET_HOURS_ENABLED`）ガードによる深夜等の自発発言自動抑止。
   - 自発発言（`_proactive_action`）冒頭の全ガード条件におけるスキップ理由のログ可視化（サイレントドロップの根絶）。
   - 自発発言プロンプト構築時におけるチャンネル文脈・要約・進行中スレッド情報の動的結合（`_build_proactive_channel_context_block`）およびエピソード記憶シードの活用。
   - 自発発言送信後における `thread_tracker.record_message` の記録とコンテキストキャッシュの安全な同期。
   - 自発発話実行時における参照元背景情報（スレッド概要、X投稿、ユーザー関心事、内省題材等）のエピソード記憶（`memory_store.add_episode`）への永続化（オンメモリキャッシュ消失後・再起動後の想起保証）。
   - サブチャンネル自発返信確率（サイコロ方式）の制御。
3. **自律ループ・定期内省・モノローグ**:
   - `loops.py` によるバックグラウンド自律タスクの制御。
   - 静かな時間帯（`BOT_QUIET_HOURS_ENABLED`）における定期内省（`_reflection_loop`）およびトピックループ（`_topic_loop`）の自動スキップ制御。
   - ユーザー対話・感情更新中のビジー判定（`_is_user_reply_busy`）による背景LLMタスク（モノローグ、自発発話、内省）の延期・抑止。
   - 定期内省ループ（`_reflection_loop`）における記憶減衰・パージ（`decay_and_purge_memories`）および行動台帳レコードの定期自動パージ（`purge_bot_activities`、デフォルト180日保持）。
   - `reflection.py` による定期内省（Deep Reflection）、ユーザー文化・信念の抽出・更新。
   - 内省時における話者ラベルマップ（`user_label_map`）の構築（記憶本文内の `ユーザー_\d+` 抽出およびDBプロファイル表示名フォールバック）。
   - Deep Reflection（`_run_deep_reflection_once`）における `num_predict` 明示指定、直近対話相手名の動的解決、および助詞を含む主語正規化連携（`Generic user subject not resolved` 棄却防止）。
   - `monologue.py` による自律思考ループ（クールダウン判定 `is_inner_monologue_in_cooldown`、スキップ理由明示、および `boredom` 自発行動へのフォールスルー）。
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
4. **自発発言（Proactive）におけるチャンネル文脈結合とスレッド継続の維持**:
   - 自発的発言（`_proactive_action`）時に、直前のチャンネル会話ログ（`channel_context_cache`）、進行中スレッド（`thread_tracker` / `memory_store`）、チャンネル要約（`get_channel_summary`）をバイパスして唐突な独り言を投稿させてはならない。必ず `_build_proactive_channel_context_block` を経由して各ソース（`memory`, `user_interest`, `x`, `img`）のプロンプトに結合すること。
   - プロンプト内で見出しや文脈テキストをオウム返し・引用させないための注意書き（`※この見出しや以下の会話ログ、分析テキストを直接オウム返し・引用・言及しないでください...`）を必ず維持すること。
   - 自発発言送信後は、`append_context_message` に加えて必ず `self.thread_tracker.record_message` を呼び出し、自発発言に対するユーザーの返信が会話スレッドとして追跡・継続できるようにすること。
   - 自発発話実行時は、セリフ履歴保存（`_store_proactive_bot_utterance`）に加えて必ず `_store_proactive_episode_memory` を呼び出し、参照した背景情報（`timeline_block`, `interest_block`, `recent_topic_hint`）をエピソード記憶（`memory_store.add_episode`）として永続化すること（オンメモリキャッシュから押し出された後や再起動後も自身が振った話題の背景を想起可能にする多重防御、BAN-28, BAN-40, BAN-117）。
   - `memory_store` の非同期メソッド呼び出しには `_safe_await`（`inspect.isawaitable`）を適用し、テスト時の同期/非同期モック双方に対応する多重防御を維持すること。
5. **ユーザー返信処理中における背景LLM推論の割り込み禁止（ビジー判定とLLM競合抑止）**:
   - `_agent_tick_loop`、`_reflection_loop`、`_topic_loop` および `_run_cancellable_background_task` において、ユーザー対話処理中（`_reply_queue` に未完了タスクがある、またはアクティブ返信中）は絶対に背景LLMタスク（モノローグ、自発発話、内省）を実行させてはならない。
   - `_is_user_reply_busy()` による判定を行い、ビジー時はログを出して処理をスキップまたは延期すること。
   - スキップ時は `safe_close_coroutine` を呼び出してコルーチンを確実にクローズし、`RuntimeWarning: coroutine was never awaited` やタスクリークを絶対に発生させないこと。
6. **モノローグ自己増殖ループ防止（BAN-114）とクールダウン時の自発行動ブロック抑止**:
   - 好奇心モノローグ（`monologue.py` の `_choose_curiosity_topic_from_memories`）において、記憶抽出候補から自身の独白・思考（`agent_thought`）、検索知識（`agent_knowledge`）、および「題材: 」「独白:」等を含むテキストを必ず除外すること。
   - 過去に独白済みのトピック（`recent_topic_hint` および `_recent_monologue_topics`）との完全一致・重複トピックをリジェクトし、自己増殖や同一題材の無限ループを絶対に発生させないこと。
   - 好奇心トピックの記憶照合（`_topic_matches_memory`）においては、完全な全体部分文字列一致だけでなく、主要名詞・固有名詞キーワード（漢字熟語、カタカナ語、複合語等）が候補記憶のいずれかに一定割合以上（60%以上等）含まれているかを判定する柔軟なアルゴリズムを適用すること。ただし、候補記憶に全く存在しない新語やハルシネーション（未登場のカタカナ語や新語等）は確実に排除する防壁を維持すること。
   - 題材生成・検証（`_clean_agent_topic_with_reason`）および選定ログにおいて、棄却理由（`reason="empty"`, `reason="length_exceeded"`, `reason="meta_phrase_detected"`, `reason="memory_mismatch"`）を明文化して詳細に出力すること。
   - `_agent_tick_loop` において、Inner Monologue がクールダウン中であることを理由に `boredom >= 0.82` の Proactive 自発行動分岐をブロックしてはならない。必ずクールダウン事前判定（`is_inner_monologue_in_cooldown`）を行い、クールダウン時は自発行動へフォールスルーさせること。
7. **自発発話の直近話題重複遮断（BAN-94）**:
   - 自発発話（`proactive.py` の `_sanitize_proactive_reply` / `_generate_proactive_reply`）において、生成された発言が直近のBot自身の発言（`channel_context_cache` 内のAssistant発言や過去の自発発話履歴 `_recent_proactive_replies`）と意味的・語句的に酷似している場合は必ずリジェクトし、再試行またはスキップを行うこと。
   - 単純な完全一致だけでなく、文字bi-gram、概念トークン（漢字・カタカナ・英数字）、熟語キーワードの重複率に基づく多重防御を維持すること。
8. **内省（Reflection / Deep Reflection）時の話者名解決と主語正規化連携（BAN-153準拠）**:
   - `reflection.py` の `user_label_map` 構築ループにおいて、各記憶の `user_id` だけでなく記憶本文（`content`）内の `ユーザー_(\d+)` を抽出して解決対象IDリストに追加すること。
   - Gatewayメンバーキャッシュ（`guild.get_member(uid)`）でメンバーが未取得、または表示名が `ユーザー_\d+` プレースホルダーの場合は、必ず `self.memory_store.get_user_profile(guild_id=guild_id or 0, user_id=uid_int)` を呼び出してDBプロファイル表示名（`current_display_name` / `current_name`）へフォールバックすること。
   - Deep Reflection（`_run_deep_reflection_once`）において、`call_ollama_json` には最初から `num_predict=cfg_int("AGENT_DEEP_REFLECTION_NUM_PREDICT", 896)` を指定して `done_reason=length` によるリトライを防止すること。
   - 直近の対話相手名をキャッシュや記憶から解決し、生成された方針（`future_policy`）の抽象主語（「ユーザーに対して」「相手には」「彼に」等）を対話相手名（`[uid:...] 『〇〇』さんに対して`）に確実に正規化して `evaluate_deep_reflection_result` を通過させること（`Generic user subject not resolved` による100%棄却の防止）。
9. **コアシステムプロンプトの完全保護、動的 `num_ctx` 拡張と連動した予算管理、およびツールカタログ保護（principles 1.14）**:
   - `_build_context` / `_prepare_context` において、コアシステムプロンプト（`parts[0]`）は絶対に切り詰めてはならない（保護領域）。
   - プロンプト予算上限（`max_prompt_chars`）の算出にあたり、固定の初期 `OLLAMA_NUM_CTX`（`16384`）だけで `12288` に縛るのではなく、プロンプト文字数・推定トークン数に応じて `_resolve_dynamic_generation_limits` が動的拡張する上限（`num_ctx=24576` や `32768` 相当、または `AGENT_MAX_PROMPT_CHARS`）を考慮して安全枠を決定すること。
   - プロンプト予算超過時でも、ツールカタログ（`【利用可能なツールカタログ】`）を削る前に、補助パーツ（要約・記憶）およびユーザープロンプト内の低優先ブロック（古い履歴や冗長な補助ブロック）を優先的にトリミングすること。
   - トリミング時も `user_prompt` 末尾の `<current_reply_target>` を含む重要ブロックは必ず保護し、どのブロックが何文字削減されたかをログ（`trimmed_blocks`）に明記すること。
10. **不要なWeb検索ツールのゲーティングと対話相手名クエリの遮断（BAN-92）**:
    - 通常の相槌や感情確認リプライ（例: `えでも好きだよね？(^○^)`）に対して `【調査思考ポリシー】` を無差別に注入してはならない。
    - LLMのツール呼び出しにおいて、クエリに対話相手名が含まれている場合、または短文相槌等の日常対話である場合は、ツール実行をスキップして直接応答させる多重ガードを維持すること。対話相手名をアニメキャラや実在人物として検索し、会話を破綻させてはならない。
11. **Proactive Action の全ガード条件におけるサイレントドロップ防止とログ可視化**:
    - `proactive.py` の `_proactive_action` 冒頭にある全ガード条件（`not is_active`, `not AGENT_PROACTIVE_POST_ENABLED`, `channel_id_value <= 0`, `idle_sec < min_idle_sec`, `cooldown`, `channel is None`）すべてにおいて、サイレントに `return` して処理をブラックホール化させてはならない。必ず `logger.info("proactive action skipped: reason=%s ...", ...)` でスキップ理由を明記すること。
    - `loops.py` の `_agent_tick_loop` においても、実態に合わせて `boredom threshold met, evaluating proactive action eligibility` とログ出力し、自発発話の追跡可能性を維持すること。
12. **静かな時間帯（生活リズム）における自発発話・背景内省スキップガード（BAN-139準拠）**:
    - `BOT_QUIET_HOURS_ENABLED` が有効な場合、`lib.date_utils.is_in_quiet_window(now_jst(), start, end)` を用いて静かな時間帯（生活リズム/睡眠時間）を判定すること。
    - 自発発言（`proactive.py` の `_proactive_action`）および背景周期ループ（`loops.py` の `_reflection_loop`, `_topic_loop`）の入口で処理をスキップし、`log.info("... skipped: reason=quiet_hours (bot is sleeping)")` を必ず記録してサイレントドロップを防止すること。
    - `BOT_QUIET_HOURS_ENABLED=False`（デフォルト）時は従来どおりの動作を維持し、後方互換性を担保すること。
13. **オーケストレータ発話記録および行動台帳定期パージ処理（過負荷・メモリリーク防止）**:
    - `engine.py` の発話メモリ保存処理において、`activity_log_enabled` が有効な場合は必ず `add_bot_activity` を呼び出して全フロント共通行動台帳に発話を記録すること（front名はcontext指定優先、無ければguild/channel有無で "x" または "discord" を判定）。
    - `loops.py` の `_reflection_loop` において、`decay_and_purge_memories` に続いて `purge_bot_activities(persona=..., retention_days=180)` を実行し、180日を超えた古い台帳レコードを自動パージすること。その際、`_is_user_reply_busy()` の譲歩プロトコルおよび安全な関数呼び出し（`getattr` / `callable`）を順守すること。
14. **ビジー判定は実在する状態だけを見る（存在しない属性を前提にした分岐の禁止）**:
    - `_is_user_reply_busy` をはじめとする競合抑止の判定は、実際に代入・維持されている状態（`_reply_queue`、`_active_normal_reply_cids` 等）だけを根拠とすること。どこでも代入されない属性（かつての `_emotion_update_tasks`）を前提にした分岐を再び入れない（§66）。
    - 感情更新（`_update_emotion_state`）は front/discord 側でメッセージ処理内にインライン実行され、タスク登録の仕組みは無い。競合抑止に組み込みたくなった場合は、登録側（front/discord）と判定側（本サブプロジェクト）を組にした別指示書で扱うこと。
15. **自発発言の失敗バックオフと話題自己循環の遮断（§83）**:
    - `proactive.py` の `_proactive_action` は既存ゲート（`quiet_hours`、`min_idle`、`cooldown`、`channel_not_resolved`）の条件と順序を保持した上で、試行単位の指数バックオフ（`_proactive_consecutive_failures` / `_proactive_last_attempt_ts`）を適用すること。重複却下・`no usable reply`・例外終了時は失敗をカウントし、次の試行まで `min(AGENT_PROACTIVE_FAILURE_BACKOFF_BASE_SEC * 2^(failures-1), AGENT_PROACTIVE_FAILURE_BACKOFF_MAX_SEC)`（既定: 30分〜6時間）未満なら `[PROACTIVE_BACKOFF] skipped: failures=%d remaining_sec=%.0f` を INFO ログ出力して生成を抑止すること。
    - 連続失敗回数は、自発発言の送信成功時または対象チャンネルでの人間発言（`last_human_message_ts` が最終試行時刻より新しい場合）に `0` へリセットすること。
    - `_build_proactive_memory_prompt` が `list_episodes` / `list_recent_episodes` から取得したエピソードを材料にする際は、`is_proactive_episode` により `_store_proactive_episode_memory` 由来の自発発言エピソード（`tags` / `PROACTIVE_EPISODE_*` 定数のラベル・サマリ形式）を除外し、会話由来エピソードのみを残すこと。`_store_proactive_episode_memory` による自発発言エピソードの保存自体は継続すること。
16. **自発発言における台帳ベースの話題重複チェック（§138）**:
    - 自発発言の重複判定（`proactive.py` の `_sanitize_proactive_reply`）は、既存のキャッシュベース文面類似度（BAN-94、`_is_proactive_reply_duplicate`）に加えて、§137 の台帳ベース話題照合（`find_discussed_topic_match`）を含めること。照合対象は全フロント・通常返信を含む直近24時間（`bot_activities` の `reply` / `post`）であること。
    - 話題照合のロジック本体は共有モジュール `harness/agent/prompt_parser/prompt_manager/discussed_topics.py` に置き、`proactive.py` へ複製しないこと（`proactive.py` 側は呼び出し・fail-open 分岐・一致時の WARNING ログ出力のみを担う）。
    - `DISCUSSED_TOPICS_ENABLED` が無効な場合、または `memory_store` 未設定・例外発生時は fail-open で話題照合をスキップして生成文を通すこと。一致時は空文字列を返して既存の再生成・見送りフローに乗せること。
17. **Bot の出力内容の背景評価（reply_eval、§156 第1.5段階）**:
    - Bot の出力内容の背景評価（reply_eval）を定期ループで行う（第1.5段階）。会話中は見送り、モデルは middle 系、無効化は `OUTPUT_EVAL_ENABLED`（既定 True）。
    - 評価ジョブ本体は `output_evaluation.py` に置き、`loops.py` 側は `_reflection_loop` から入口 `maybe_run_output_evaluation(self)` を呼ぶ最小変更に留めること（ループ起動は front/discord 側の管轄のため、既存の `_reflection_loop` に相乗りする）。送信経路には一切手を入れないこと。
    - 対象は過去24時間の `bot_activities` のうち `kind` が `reply` / `post` で対応する `reply_eval` が無いもの。古い順に `OUTPUT_EVAL_MAX_PER_RUN`（既定10）件まで、`OUTPUT_EVAL_INTERVAL_SEC`（既定1800秒、実効間隔は `REFLECTION_TICK_INTERVAL_SEC` 以上）ごとに処理する。`ACTIVITY_LOG_ENABLED` も有効なときのみ動く。
    - 記録形式（§155・§157 と共通の契約）: `kind="reply_eval"`、`front` / `persona` は対象と同じ、`summary` は対象の summary（最大400字）、`detail` は JSON（最大800字）`{"target_ref", "target_kind", "stance", "labels"}`。`target_ref` は対象の `source_ref`、無ければ対象レコードの `id`。`stance` は positive / negative / neutral / mixed、`labels` は lecture / gloss / verification_hedge / playful / apology / factual / question_answer の固定集合。
    - 既評価判定は reply_eval の `target_ref` と `summary` の組で突き合わせること（ふたばのように複数投稿が同じ `source_ref` を共有しても別投稿を既評価と誤認しないため）。
    - §133 会話活動レジストリが会話継続中を示す間は、その回を丸ごと見送り対象を消費しないこと（レジストリ参照不可時は fail-open で実行）。実行途中で会話が始まった場合も残りは次回へ回すこと。ユーザー返信中（`_is_user_reply_busy`）も見送り、実行は `_run_cancellable_background_task` 経由で背景LLMタスクの単一実行ガードに乗せること。
    - モデル名は `OLLAMA_MIDDLE_MODEL` 経由でのみ解決し、解決できなければその回をスキップすること（廃止済みモデル名を既定値にしない、§140）。1件1回のモデル呼び出しで固定 JSON スキーマ `{stance, labels}` を返させ、パース失敗の件は記録せず次回再試行できる状態に残すこと。
    - 1件以上記録した実行では INFO ログ `[OUTPUT_EVAL] evaluated: count=%d` を1行出すこと。例外はすべて握ってループを止めない（失敗は warning ログ1行）。
18. **第2段階の対照レッスン抽出（lesson、§158 第2段階・改訂1）**:
    - 第2段階の対照レッスン抽出（lesson）を定期ループで行う。材料は `gate_incident` / `gate_pass` と `post_outcome` の対照に加え、`discord_reply` では `target_text` のある `reply_feedback` を正式な対照材料とする（改訂1、無効化は `LESSON_FEEDBACK_MATERIAL_ENABLED`、既定 True）。生成への注入はまだ行わない。会話中は見送り、モデルは middle 系、無効化は `LESSON_EXTRACTION_ENABLED`（既定 True）。
    - 抽出ジョブ本体は `lesson_extraction.py` に置き、`loops.py` 側は `_reflection_loop` から入口 `maybe_run_lesson_extraction(self)` を呼ぶ最小変更に留めること。生成・送信・検査の挙動は変更しないこと。
    - 対照材料は過去 `LESSON_WINDOW_DAYS`（既定7日）から persona 別・scope 別（`discord_reply` / `hutaba_post`）に収集する。`discord_reply` では `LESSON_FEEDBACK_MATERIAL_ENABLED=True` かつ `detail.target_text` を持つ `reply_feedback` を正式材料とし、`signals` に `negative` / `correction` / `incomprehension` のいずれかを含む記録（`positive` 併存時も苦情優先）を負例、`positive` のみの記録を正例として新しい順に最大 `LESSON_FEEDBACK_MAX_EXAMPLES`（既定10）件まで採用する。正規化発言文が `gate_incident` / `gate_pass` と一致するフィードバック記録は独立計上せず、反応文と `signals` を一致したゲート材料側へ併記する。`target_text` の無い `reply_feedback`（または無効時）は従来どおり任意補強扱いに落とす。`hutaba_post` の `post_outcome` は `(source_ref, detail.post_no)` ごとの最新スナップショットで判定し、正例は `soudane >= 3` または `reply_count >= 2`、負例は `soudane == 0` かつ `reply_count == 0` かつ初回観測から24時間以上経過したもののみを採用する。`reply_eval` / `target_text` の無い `reply_feedback` / `post_feedback` が紐付く場合は任意で補強材料に添える。
    - 最低根拠条件（`LESSON_MIN_EVIDENCE`、既定3）: `avoid` は負例3件以上かつ正例1件以上、`prefer` は正例3件以上かつ負例1件以上を満たさない scope は見送る（フィードバック由来も件数に含める）。モデル名は `OLLAMA_MIDDLE_MODEL` 経由でのみ解決し（§140）、パース失敗の scope は記録せず次回再試行可能に残す。
    - 記録形式: `kind="lesson"`、`front` は材料側（`"discord"` / `"hutaba"`）、`source_ref="stage2"`、`summary` は教訓文（最大400字）、`detail` は JSON（最大800字）`{"scope", "polarity", "evidence_neg", "evidence_pos", "feedback_neg", "feedback_pos", "window_days"}`（ふたば材料の `feedback_neg` / `feedback_pos` は `0`）。過去90日の同一 persona・scope の既存 lesson と類似度 0.7 以上（`calculate_utterance_similarity` 優先）なら重複抑止し、1回の上限は `LESSON_MAX_PER_RUN`（既定3件）、実行間隔は `LESSON_EXTRACT_INTERVAL_SEC`（既定86400秒）。`ACTIVITY_LOG_ENABLED` も有効なときのみ動く。
    - §133 会話活動レジストリが会話継続中を示す間は実行を見送り（参照不可時は fail-open）、保存1件ごとに INFO ログ `[LESSON] stored: scope=%s polarity=%s lesson=%s` を1行出すこと。例外はすべて握ってループを止めない（失敗は warning ログ1行）。
19. **第4段階の自発発言候補比較選定（candidate_compare、§166 第4段階）**:
    - 第4段階として、自発発言の下書きを複数生成し、既存検証を通した候補から類似度の低さで最良を選ぶ。無効化は `PROACTIVE_CANDIDATE_COMPARE_ENABLED`（既定 True）。
    - `proactive.py` の `_generate_proactive_reply` 内で、`PROACTIVE_CANDIDATE_COMPARE_ENABLED=True` かつ `PROACTIVE_CANDIDATE_COUNT`（既定3）が2以上のとき、合計 `PROACTIVE_CANDIDATE_COUNT` 本まで下書きを生成し、各下書きを既存の `_sanitize_proactive_reply`（BAN-94 重複チェック・§138 既出話題チェックを含む）に通す。既存の再試行プロンプト（`build_output_only_retry_prompt`）および `_proactive_action` の memory フォールバックは壊さず、比較はその内側に収めること。
    - 選定の物差し（§165・§167 と共通契約）: 検証で落とされた下書きは硬く棄却し、通過候補が2本以上の場合は直近のBot自身の発言群（`_get_recent_assistant_utterances`）に対する `calculate_utterance_similarity` の最大値が最小の1件を選ぶ（同点は生成順優先）。生成候補が1本のみの場合はそのまま投稿、全棄却なら従来通り投稿を見送る。
    - 比較記録: `memory_store.add_bot_activity` へ `kind="candidate_compare"`, `front="discord"`, `persona=cfg_persona_namespace(custom_cfg=cfg)`, `source_ref="stage4"`, `summary`（選定下書き・全棄却時は最初の下書き、最大400字）, `detail`（JSON最大800字: `{"scope": "proactive_discord", "chosen", "candidates": [{"head", "verdict"}, ...]}`）を記録し、成功時に INFO ログ `[CANDIDATE_COMPARE] scope=proactive_discord chosen=%d total=%d` を1行出力する。生成・検証・比較・記録の例外はすべて fail-open で握り、比較失敗時は最初の通過候補を投稿すること。
20. **反省ループの失敗バックオフとプロンプト JSON 強制（§176）**:
    - 通常反省・深い反省の system プロンプトには、orchestrator 側のラップ（`reflection_backoff.py` の `apply_json_only_suffix`）で「JSON のみ出力＋最小出力例」のサフィックスを末尾付加すること。`memory_manager` 側のプロンプト定数は編集しない。無効化は `REFLECTION_JSON_ONLY_SUFFIX_ENABLED`（既定 True）。
    - 通常反省のターゲットループは、材料（`memory_lines`）の fingerprint が前回失敗時と同一の対象について、LLM を呼ばずにスキップすること（`ReflectionFailureTracker`、状態はメモリ上のみ・再起動で消去）。成功したら該当キーの記録を消去し、材料が変われば再試行する。無効化は `REFLECTION_FAILURE_BACKOFF_ENABLED`（既定 True）で、その場合は従来動作と完全一致すること。深い反省はバックオフ対象外（サフィックスのみ）。
    - バックオフによるスキップ時は INFO ログ `[REFLECTION_BACKOFF] skip unchanged failed target: ...` を必ず出すこと（サイレントドロップ防止）。

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
