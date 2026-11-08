# 🤖 Memory Manager ガイドライン (GEMINI.md)

本ディレクトリ（`harness/agent/memory_manager/`）は、AIエージェントの**メモリ管理（短期記憶・長期記憶/RAG・要約保存）**を担当するサブプロジェクトです。

詳細な開発ガイドラインおよび境界条件は、正本である [AGENTS.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/memory_manager/AGENTS.md) を参照してください。

> [!IMPORTANT]
> **最優先原則**:
> 1. **境界条件遵守**: 自身のディレクトリ外（`harness/agent/memory_manager/` 外）のファイルを直接編集してはならない。
> 2. **ハルシネーション遮断**: 感情理由や別文脈からの事実捏造保存を事前ガードで徹底的に防止すること。
> 3. **アトミック書き込み**: DB永続化時の整合性と耐障害性を死守すること。
> 4. **静的型検査**: Python 3.11+ 型アノテーションと `mypy` エラー0件を維持すること。
> 5. **不要参照排除**: 未使用インポートや旧エイリアス（`extract_long_term_memories` 等）の再導入を防止すること。
> 6. **管理基盤維持**: `storage/` 配下の将来用・管理用メソッドを勝手に削除しないこと。
> 7. **チャンネル要約有効性**: `retriever.py` の要約取得デフォルト有効（`True`）を維持すること。
> 8. **要約行数確保とキャッシュ互換**: `post_reply_handler.py` の文脈切り詰め（30行）および `dict[int, deque]` キャッシュ対応を維持すること。
> 9. **トピック永続化維持**: `topic_labels` および `key_topics` の永続化・型整合性を維持し、デッドコード化を防止すること。
> 10. **反省器二重敬称防止**: `build_reflection_memory_lines` での話者敬称二重化（`[〇〇さんさん]` 等）を防止すること。
> 11. **記憶抽出モデル軽量優先と計測ログ**: `extract_and_store_memory` で `MEMORY_EXTRACT_MODEL` 未指定時は `MIDDLE_LLM_MODEL` / `OLLAMA_MIDDLE_MODEL` / `SUB_LLM_MODEL` を優先し、メインモデル（27B）直接フォールバックによるGPU長時間占有を防止するとともに、抽出開始・完了時に `model=%s elapsed=%.2fs` のINFOログを出力すること。
> 12. **ユーザー実名ニックネーム解決**: `user_display_name` 未指定時も `user_profiles` テーブルから解決し、本文・プロンプト・DB内の `『ユーザー_<ID>』` プレースホルダー残存を徹底排除すること。
> 13. **内省チャンネル主語正規化**: 特定個人に帰属できない内省信念の抽象主語（`ユーザーは/が` 等）を `このチャンネルでは` に正規化し、主語一般化バグと不要なバリデーション却下を防止すること。
> 14. **会話習慣プロファイル時系列逆転遮断・根絶と生IDマスク (#158, #162, #165)**: `update_user_habit_profile` において `[AI返答]` をプロンプトから除外し、直前文脈からのユーザー発言切り出しに限定。URL単体投稿時は早期スキップ（`None` 返却）するとともに、多重防御として `_looks_like_reversed_timeline_habit_reason` で「〜で返答した際」「〜を否定せず」「反応を示し」「挑発的な冗談を続け」等を含む因果逆転構文を完全遮断し、ログ出力（`Skipped habit profile update due to reversed timeline hallucination` 等）では17〜20桁のDiscord生IDやユーザー名を確実にマスクすること。
> 15. **文脈行のBot絵文字揺れ重複排除**: `trim_memory_context_lines` において `(>>😋...)` と `(>>🤩...)` の差分を正規化して重複行を排除し、リアクション付き行を優先保持すること。
> 16. **Deep Reflection 主語ルール徹底と一人称汎用化**: `DEEP_REFLECTION_SYSTEM_PROMPT` および `build_deep_reflection_prompt` で特定キャラ名の直書きを排除して任意の一人称に対応させ、`future_policy` の漠然主語（ユーザーが/は）を厳禁して特定名または自身の一人称（例: 私は〜、自分は〜等）を強制し Evaluator 棄却を防止すること。
> 17. **返信優先譲歩と構造化ログ**: `handle_post_reply_memory_and_summary` の各LLMステップ開始前に `_is_user_reply_busy()` 待機で本応答推論に譲歩し、`[post-reply] Step=%s status=%s elapsed=%.2fs` を漏れなく出力すること。
> 18. **第三者体型揶揄・下ネタ悪ノリ遮断**: `_is_single_turn_unsafe_or_low_value` および抽出後バリデーションにおいて、「おっぱい」「ちんこ」「まんこ」「エロ」「セクハラ」や第三者体型揶揄（「太っている」「デブ」等）の `fact` 記憶化および `training_candidate.should_keep = True` を強制棄却（`should_keep = False`、記憶スキップ）し、`Skipped memory/training candidate due to unsafe/banter filter: reason=%s` をログ出力すること。
> 19. **チャンネル横断記憶検索・エピソード補完・ローリング要約・受動的記憶保持**:
>     - `core_store.py` の `search_memories_*` で `include_same_channel_users=True` をサポートし、同一チャンネル内他者の記憶（食事・予定等）も取得可能に維持すること。
>     - `retriever.py` の `MEMORY_INCLUDE_CHANNEL_MEMORIES_IN_REPLY` デフォルト有効（`True`）、フォールバック時ボーナス付与、および候補不足時の `memory_episodes`（`[過去の出来事] ...`）最大2件補完を維持すること。
>     - `summarizer.py` の `maybe_update_channel_summary` で前回の要約を引き継ぎ用として渡し、数時間前の宣言・エピソードのローリング保持を行うこと。
>     - `extractor.py` の `maybe_extract_passive_channel_memory` で未返信ログから食事・予定等の実質発言を軽量ルール選別し、Bot未返信（`assistant_text=""`）として発言者名を主語にした長期記憶を受動的に保持すること。
> 20. **掲示板スラング・接頭辞誤認防止とクエリサニタイズ**: `slang.py` で定義された「すみフラ」「うろフラ」等の prefix_slang を固有名詞として扱わず、`filter_query_terms_by_slang` で検索クエリから確実に除外し、`format_slang_guidance_for_prompt` で注意書きを注入して誤読・捏造を防止すること。
