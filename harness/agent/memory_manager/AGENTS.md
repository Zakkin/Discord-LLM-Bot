# 🤖 Memory Manager サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/memory_manager/`）は、AIエージェントの**メモリ管理（短期記憶・長期記憶/RAG・要約保存）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **永続化と検索基盤（RAG）**:
   - `MemoryStore` による SQLite / sqlite-vec / FTS5 ハイブリッド記憶データベース管理。
   - ベクトル類似度検索、全文検索、時間減衰スコアリング、関連記憶の取得（`retriever.py`）。
2. **記憶抽出・バリデーション**:
   - `extractor.py` による対話ログからの長期記憶（fact, preference, culture等）の抽出。
   - ハルシネーション（感情理由からの引き写し、架空エピソード、第三者混同）の保存前多重防御ガード。
3. **プロファイルと要約の更新**:
   - 習慣プロファイル（`update_user_habit_profile`）、ユーザー興味関心、チャンネル要約（`maybe_update_channel_summary`）。
   - ユーザー表示名解決（`user_identity_resolver.py`）。
4. **返信後メモリ永続化ハンドラー**:
   - `post_reply_handler.py` によるフェーズ5完了後の安全な長期記憶抽出、エピソード保存、スレッド永続化。
5. **掲示板・ネットスラング管理（BBS Slang Manager）**:
   - `slang.py` による掲示板スラング（「すみフラ」「うろフラ」「なそ」等）のマスター辞書管理。
   - スラングの検出（`lookup_slang_in_text`）、検索クエリサニタイズ（`filter_query_terms_by_slang`）、およびプロンプト注記テキスト生成（`format_slang_guidance_for_prompt`）。
   - 前置き接頭辞（prefix slang）の固有名詞誤読・検索ノイズ化（フラダンス等への誤認・知ったかぶり捏造）の徹底防止。
6. **期日想起およびユーザーデータ管理（プライバシー・開示・物理削除）**:
   - `memory_prospective` の期日（`due_at`）に基づく時間発火想起取得（`list_due_prospective_memories`）。
   - ユーザーデータの完全物理削除（`delete_user_data`）および記憶概要取得・開示（`get_user_memory_overview`）。
7. **会話状態メモ（短期・作業用会話状態管理）**:
   - `conversation_states` テーブルによる、ターンごとの「現在の前提状態」（`topic`, `established_facts`, `denied_claims`, `open_questions`, `user_mood`）の永続化と取得（`get_conversation_state`, `set_conversation_state`）。
   - 長期用のチャンネル要約（`channel_summaries`）と明確に責務を分離し、短期・作業用前提の誤前提引きずられ破綻を防止。
8. **自己言及リコール向け発話根拠（`reference_note`）の永続化と詳細取得**:
   - `bot_utterances` テーブルにおける発話根拠メモ（外部コンテンツ言及、タイムライン閲覧情報等、最大800文字）の永続化（`add_bot_utterance`）。
   - 直近のBot発言とその言及根拠メモを降順で取得する詳細API（`get_recent_bot_utterances_detailed`）。
9. **全フロント横断活動台帳（Bot Activities）の永続化と検索・パージ**:
   - `bot_activities` テーブルによる、Discord / ふたば / X 等の全フロントにおけるBotの体験・行動（発言・投稿・リアクション・閲覧等）の横断記録（`add_bot_activity`）。
   - フロント（`fronts`）や行動種別（`kinds`）、期間（`since`, `until`）を指定した横断検索API（`search_bot_activities`）。
   - 保持期間（`retention_days`、デフォルト180日）超過レコードの安全なパージ（`purge_bot_activities`）。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/memory_manager/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `tool_registry/`, `observation/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **アトミック書き込みの遵守**:
   - データベースやJSONファイルの永続化において、一時ファイル `fsync` ➜ `replace` によるアトミック操作を行い、クラッシュによるデータ破損を絶対に防ぐこと。
3. **長期記憶抽出における事実捏造（ハルシネーション）の遮断**:
   - 直前の感情理由（`emotion_state.reason`）や推論思考内の仮定を「ユーザーとの約束・事実」として捏造保存してはならない。
   - 会話ソースに根拠のない主要キーワードを含む記憶を保存前ガード（`_is_unsupported_hallucinated_memory` 等）で確実に除外すること。
4. **ユーザー教示文における無関係記憶全選択の防止**:
   - ユーザーが「〜とは…です」と定義を教えている文面に対して、無関係な過去記憶を無差別に全選択して混入させてはならない。
5. **未使用インポートおよび廃止旧エイリアスの再導入禁止**:
   - `logic.py` などのファサードモジュールにおいて、個別インポート済みで参照のない未使用モジュールインポート（過去の `user_identity_resolver`, `post_reply_handler` 残骸等）を混入させないこと。
   - `extractor.py` の `extract_long_term_memories` 等の旧関数名エイリアスは完全に廃止済みである。過去のエイリアスを再定義・再導入せず、常に正本（`extract_and_store_memory`）を参照すること。
6. **将来用・管理用ストレージ基盤メソッドの保護**:
   - `storage/learning.py`, `storage/interactions.py`, `storage/profiles.py` の各種メソッド（`list_training_candidates`, `get_active_channel_threads` 等）は将来用および運用管理用基盤として意図的に維持されている。現時点で参照が少ないからといって自己判断で勝手に削除・破壊してはならない（※なお、未参照デッドコードとなっていた旧ユーザー解決ヘルパー `find_users_by_name_or_alias` / `get_user_aliases_for_user` は `user_identity_resolver.py` および `get_user_profile` への一本化に伴い安全に廃止・削除済み）。
7. **返信プロンプトでのチャンネル要約取得のデフォルト有効性維持**:
   - `retriever.py` の `get_relevant_memories` において、`include_channel_summary` が未指定（`None`）の際の参照設定 `MEMORY_INCLUDE_CHANNEL_SUMMARY_IN_REPLY` のデフォルト値は必ず `True` を維持すること。安易に `False` へ戻して返信プロンプトから `[これまでのチャンネル要約]` が消失するデグレを絶対に再発させないこと。
8. **チャンネル要約トリガー行数の確保とコンテキスト切り詰めの責務分離**:
   - `post_reply_handler.py` の `trim_memory_context_lines` において、デフォルト行数は要約判定に必要な長文脈 `MEMORY_LONG_CONTEXT_LINES`（デフォルト30行）を維持すること。記憶抽出用の短い切り詰め（デフォルト6行）と要約用文脈を混同して一括切り詰めを行ってはならない。
   - `channel_context_cache` からの行取得時は、`.get_recent_lines()` メソッドだけでなく、実体が `dict[int, deque]` や `list`（要素が辞書形式 `{"line": ...}` または文字列）である場合にも耐えられるよう柔軟なフォールバック構造を維持すること。
9. **トピックラベルおよびキー・トピックの永続化・スキーマ整合性の維持**:
   - `types.py` の `ChannelSummaryRecord.topic_labels` および `MemoryEpisode.key_topics` をデッドコード化させないこと。
   - `storage/interactions.py` の `set_channel_summary` / `get_channel_summary` / `list_channel_summaries` は常に `"topic_labels"`（`list[str]`）を保持・返却し、`add_episode` / `list_recent_episodes` / `list_episodes` は常に `"key_topics"`（`list[str]`）を保持・返却するインターフェースを死守すること。
   - `summarizer.py` の要約更新ロジックおよび `post_reply_handler.py` のエピソード保存ロジックから、トピック情報が確実に渡される経路を損なわないこと。
10. **反省器プロンプトにおける二重敬称防止**:
   - `reflection.py` の `build_reflection_memory_lines` において、話者名（`speaker_name`）が既に「さん」「ちゃん」「くん」「様」「氏」等の敬称で終わっている場合は、末尾に重ねて「さん」を付加しないこと（`[〇〇さんさん]` などの敬称二重化バグを絶対に再発させないこと）。
11. **記憶抽出モデルの軽量/中量モデル優先化と計測ログ**:
   - `extractor.py` の `extract_and_store_memory` において、`MEMORY_EXTRACT_MODEL` が未指定（または空）の場合に重量級メインモデル（`OLLAMA_MODEL` = 27B）へ直接フォールバックさせず、必ず `MIDDLE_LLM_MODEL` / `OLLAMA_MIDDLE_MODEL` / `SUB_LLM_MODEL`（軽量/中量モデル）を優先参照すること。返信後処理による長時間のGPU占有やレイテンシ悪化を絶対に防止すること。
   - 記憶抽出の開始・完了時に使用モデル名と所要時間（`model=%s elapsed=%.2fs`）を `INFO` ログに出力すること。
12. **『ユーザー_<ID>』プレースホルダーの記憶保存・プロンプト残存の厳禁**:
   - 呼び出し元から `user_display_name` が渡されない経路、または `ユーザー_\d+` プレースホルダーのままの場合でも、`store.get_user_profile`（`current_display_name`）や `resolve_user_display_name` から実名サーバーニックネームを動的に解決すること。
   - `normalize_memory_text`、`build_reflection_memory_lines`、`normalize_reflection_belief` において、本文中の `『?ユーザー_\d+』?(?:さん|くん|ちゃん|氏|様)?` を解決済みの実名ニックネーム（`『{base_name}』さん`）に確実に置換し、DBやプロンプト内にIDプレースホルダーを残存させないこと。
13. **内省におけるチャンネル全体信念の抽象主語正規化**:
   - 特定個人に帰属できないチャンネル全体内省（`subject_kind == "agent"` または話者特定不能）において、文頭・読点直後・文中の抽象主語（`ユーザーは/が/も`、`相手は/が/も`、`みんなは/が/も`、`このチャンネルのユーザー達は` 等）を放置して保存したりバリデーションで不用意に弾かせず、必ず `"このチャンネルでは"` に正規化すること。
14. **会話習慣プロファイル更新における時系列・因果関係逆転ハルシネーション根絶とガード強化（#158, #162, #165）**:
   - `update_user_habit_profile` のプロンプトにおいて、時系列逆転ハルシネーションの根本原因となる `[AI返答]`（`assistant_text`）の注入を廃止し、`[直前の会話履歴]`（`[直近文脈]`）の流れを受けてユーザーが発言をどう切り出したかのみを分析する構成にすること。
   - `_looks_like_reversed_timeline_habit_reason` において、以下の時系列逆転および混同ハルシネーションを多重防御として確実に捕捉・棄却すること：
     1. 読点 `、` や長めの修飾句（`[^。\n]{0,60}`）を挟んだ接続句（`に対し(て)?`、`に反応して`、`を受けて`、`にツッコ`、`に答え`、`に返答` 等）に加え、「AIが〜で返答した際」「発言した際」「言った際」「したとき/時」「ツッコミを入れた時」「を否定せず」「を受け入れ」「に共感」「に肯定」「に同調」「反応を示し」「挑発的な冗談を続け」等の逆転パターン。
     2. 引用符の有無を問わず、AI主語＋発話/返答動詞＋「した際/したとき/した時」＋ユーザー反応が続く時系列逆転構文。
     3. `reason` 内でユーザーの発言・反応・共感の文脈（`「...」という文脈で共感` 等）として引用されているフレーズが、本来のユーザー発言ではなく `assistant_text` 側にのみ存在するフレーズである場合のAI発言混同パターン。
   - `summarizer.py` のログ出力（特に `Skipped habit profile update due to reversed timeline hallucination` 等）において、LLM出力に含まれる17〜20桁のDiscord生ID（`\b\d{17,20}\b`）やユーザー名をマスクして出力すること。
   - ユーザー発言から URL（およびシステム自動付与のメディア補足・リンク要約テキスト）を除いた実質的なユーザー自身のテキストが空または記号のみである場合（URL単体投稿時）は、口調やジョーク受容度を判断できないため `update_user_habit_profile` の LLM 呼び出し自体を早期スキップ（`None` 返却）すること。
15. **文脈切り詰めにおける返信先Bot絵文字揺れ重複排除**:
   - `post_reply_handler.py` の `trim_memory_context_lines` において、`(>>😋 AI...)` と `(>>🤩 AI...)` のように `(>>...)` 内の感情絵文字だけが異なる同一発言行を正規化ベースキーで重複排除し、リアクション情報付きの行を優先保持すること。
16. **Deep Reflection プロンプトにおける主語ルールの徹底と一人称記述の汎用化**:
   - `reflection.py` の `DEEP_REFLECTION_SYSTEM_PROMPT` および `build_deep_reflection_prompt` において、特定キャラ名の直書きを排除し、任意の一人称に対応した汎用プロンプトとして維持すること。`future_policy` や `belief` の主語に漠然とした「ユーザーが/は」を使うことを厳禁し、特定の相手なら『〇〇さんが』、対話全般の自分の行動指針なら『自身の一人称（例: 私は〜、自分は〜等）』や『相手の発言に対して〜』と記述することを明記すること（Evaluator による棄却の根絶）。
17. **返信後バックグラウンドタスクのユーザー返信推論優先譲歩と構造化ログ出力**:
   - `post_reply_handler.py` の `handle_post_reply_memory_and_summary` における各LLMステップ（長期記憶抽出、Deep Reflection、習慣更新、関心事更新、要約更新）の開始前に、`bot_cog._is_user_reply_busy()` が `True` の場合は完了まで小刻みに待機（譲歩）し、本応答のGPU推論との競合・遅延を確実に抑止すること。
   - 各ステップの開始・完了・スキップ・失敗時に `[post-reply] Step=%s status=%s elapsed=%.2fs` をログ出力すること。
18. **第三者の体型・外見揶揄および性的・下ネタ悪ノリ発言のfact記憶化・学習候補化遮断**:
   - `_is_single_turn_unsafe_or_low_value` および抽出後バリデーションにおいて、「おっぱい」「ちんこ」「まんこ」「エロ」「セクハラ」等の下世話な身体部位・性的表現や、第三者の体型・外見（「太っている」「デブ」「ブサイク」等）を揶揄・悪ノリする単発発言について、`memory_type == "fact"` での長期記憶保存および `training_candidate.should_keep = True` を強制的に棄却（`should_keep = False`、記憶スキップ）すること。
   - 棄却時は `logger.info("Skipped memory/training candidate due to unsafe/banter filter: reason=%s", ...)` をログ出力すること。
19. **チャンネル・掲示板横断記憶検索、エピソード補完、ローリング要約引き継ぎ、および受動的記憶保持**:
   - `storage/core_store.py` のベクトル検索（`search_memories_by_vector`）、全文検索（`search_memories_by_keyword`）、ハイブリッド検索（`search_memories_hybrid`）において、`include_same_channel_users: bool = True` をサポートし、同一チャンネル・スレッド内の他ユーザーの記憶（食事・予定・行動宣言等）も検索・参照可能に維持すること。
   - `retriever.py` の `get_relevant_memories` において、`MEMORY_INCLUDE_CHANNEL_MEMORIES_IN_REPLY` のデフォルト値を `True` とし、フォールバック検索時も同チャンネル記憶を通過させてボーナスを付与すること。また記憶候補不足（特に0件）かつ `user_text` が指定されている際は、同チャンネルの直近エピソード記憶（`memory_episodes`）からキーワード部分一致するエピソード（`[過去の出来事] ...`）を最大2件補完すること。
   - `summarizer.py` の `maybe_update_channel_summary` において、前回の `channel_summary` を取得してプロンプト（`[これまでの要約（引き継ぎ用）]`）へ渡し、数時間前の具体的な出来事や予定・宣言が上書き消去されないようローリング保持すること。
   - `extractor.py` の `maybe_extract_passive_channel_memory` において、未返信ログから食事・行動宣言・予定・好み等の実質的発言を軽量ルールで選別し、Bot未返信（`assistant_text=""`）として発言者名を主語にした長期記憶（`memories`）を受動的に抽出・保存すること。単なる挨拶・草・スタンプ・URL単体等のノイズ発言は即スキップしてLLM負荷を抑止すること。
20. **掲示板スラング・接頭辞の固有名詞誤読および検索ノイズ化防止（#47, #50）**:
   - 「すみフラ」「うろフラ」等の掲示板前置き接頭辞（`category == "prefix_slang"` または `strip_from_query == True`）をキャラクター名や作品名などの固有名詞として扱わないこと。
   - Web検索クエリ生成時には `filter_query_terms_by_slang` を通じてこれらを除外し、無関係な概念（「フラ」→フラダンス等）への誤認やハルシネーション検索を未然に遮断すること。
   - プロンプト生成時には `format_slang_guidance_for_prompt` により適切な注記を付与し、モデルが誤認しないようガイダンスを提供すること。
   - **SQLite DB (memory.db) 永続化と自動Seed基盤**:
     - `bbs_slangs` テーブルにスラング辞書（20選以上）を格納・永続化し、起動時・初期化時に `seed_default_slangs` により `INSERT OR IGNORE` で安全に自動シードすること。
     - スラングの追加・更新（CRUD）は `upsert_slang`（`ON CONFLICT(term) DO UPDATE`）により透過的に行い、`load_slangs_from_db` によりインメモリキャッシュ `_SLANG_CACHE` とDBを常時同期すること。
     - `MemoryStore` のインスタンスメソッド（`store.seed_default_slangs()`, `store.upsert_slang()`, `store.load_slangs_from_db()`）および `logic.py` ファサードから透過的に呼び出し可能に維持すること。
21. **prospective memory 期日管理とユーザーデータ物理削除・開示（BAN-183）**:
    - `memory_prospective` テーブルの `due_at` カラムによる時間発火リマインド管理。`list_due_prospective_memories` は `status='pending'` かつ `due_at IS NOT NULL` かつ `due_at <= now_iso` のレコードを優先度降順（priority DESC）、期日昇順（due_at ASC）で取得すること。
    - ユーザーのプライバシー保護および誤記憶自己修復のための物理削除API `delete_user_data(persona=..., guild_id=..., user_id=...)` において、`memories` テーブルの削除時に対応する `fts_memories` (rowid) および `vec_memories` (memory_id) を必ず同一トランザクション内で連動削除すること（BAN-183）。
    - 関連テーブル（`memory_episodes`, `memory_prospective`, `user_habit_profiles`/`user_habit_records`, `user_interests`, `reflections` (`subject_kind='user'`), `user_relationship_logs`, `feedback_items`, `training_candidates`）も確実に同一トランザクションで物理削除し、テーブルごとの削除件数辞書を返却すること。
    - ユーザー記憶開示API `get_user_memory_overview` において、直近の記憶一覧（`id`, `text`, `created_at`）、総件数、およびプロファイル情報を安全に返却すること。
22. **会話状態メモ（短期・作業用）とチャンネル要約（長期用）の責務分離（BAN-143）**:
    - `conversation_states`（短期・作業用）と `channel_summaries`（長期用）を明確に責務分離すること。
    - 会話状態メモは、ターンごとに更新される直近の作業前提（確定事項 `established_facts`、否定事項 `denied_claims`、未解決疑問 `open_questions` 等）を保持し、文脈ログ肥大化時の誤前提引きずられ破綻を防止する。
    - 返信後直列化枠組み（BAN-143）を厳守し、`get_conversation_state` / `set_conversation_state` は非同期かつ安全に動作すること。メタキー（`_` 始まり）は保存時に除外し、取得時にメタ情報（`_turn_count`, `_updated_at`）を透過的に復元すること。
23. **自己言及リコール向け発話根拠メモ保存と詳細取得（BAN-166, BAN-170）**:
    - `bot_utterances` テーブルのマイグレーションは `_ensure_column` により安全・アトミックに行い、既存DBとの整合性を死守すること（BAN-166）。
    - `add_bot_utterance` の `reference_note` 引数はオプショナル（デフォルト `None`）とし、既存呼び出し元への後方互換性を100%維持すること（BAN-170）。
    - `get_recent_bot_utterances` は既存のシグネチャ・戻り値（`list[str]`、時系列昇順）を完全に維持し、言及根拠が必要な呼び出し元は新設の `get_recent_bot_utterances_detailed`（`list[dict[str, str]]`、降順）を使用すること。
24. **全フロント横断活動台帳（bot_activities）の永続化と検索基盤**:
    - `bot_activities` テーブル（`id`, `persona`, `front`, `kind`, `summary`, `detail`, `guild_id`, `channel_id`, `source_ref`, `created_at`）による横断想起基盤の維持。
    - `summary` は必須（空文字禁止、最大400文字）、`detail`（最大800文字）、`source_ref`（最大200文字）としてサニタイズ・切り詰めを行い安全に格納すること。
    - `search_bot_activities` において、`fronts` や `kinds` の小文字正規化および `IN` 句プレースホルダーによる安全なパラメータバインドを行うこと。
    - `purge_bot_activities` において、`retention_days`（最小1日）に基づくUTC時刻基準でのパージをトランザクション内で実行すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest harness/agent/memory_manager/tests/test_conversation_states_storage.py harness/agent/memory_manager/tests/test_prospective_due_and_delete.py harness/agent/memory_manager/tests/test_slang.py harness/agent/memory_manager/tests/test_cross_channel_passive_memory.py harness/agent/memory_manager/tests/test_memory_manager_habit_timeline_guard.py harness/agent/memory_manager/tests/test_reflection.py tests/test_memory_logic.py tests/test_memory_store.py
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/memory_manager
  ```
