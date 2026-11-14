# 🤖 Prompt Parser サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/prompt_parser/`）は、AIエージェントの**フェーズ1（Context構築）・フェーズ2（Thought/計画立案）・フェーズ5（Final Response生成）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **フェーズ1（Context構築）**:
   - システムプロンプトの読み込みと動的構築（ペルソナ、ルール、日時コンテキスト、掲示板向けプロンプト `board_prompts.py`）。
   - 掲示板（ふたば☆ちゃんねる）スレッド内被返信関係の集計（`calculate_img_thread_reply_counts`）、要約整形（`format_img_thread_summary`）、本文引用逆引きによる親子関係ラベル化（`[>>XX への返信]` / `[スレッド全体への独立発言]`）、孤立発言への返信非推奨ラベル（`[独立発言・返信非推奨]`）付与、マクロ話題遷移要約（`macro_topic_summary`）の注入、進行中スレッドにおける注目の会話レス限定引用・空配列厳禁プロンプト設計（`build_img_thread_post_prompt`）、修辞的疑問（〇〇はどう思う？）への傲慢な代弁ボケ・スレ内定型句投下の許可、大喜利時のペルソナスイッチング（語尾減衰）、キメラ返信禁止ルールの明確化（周囲のミーム・ノリの横展開許可）、JSONの `comment` フィールドへの非アンカー引用行（`>本文`）混入防止ルール、直前レス暗黙文脈（`_is_implicit_context`）フォールバック保持（`build_img_thread_reply_context_chain`）、システムプロンプトの3段階推論（Step A/B/C）とJSONキー順序（`thought` 先行出力）誘導、および重複ルール整理による推論高速化。
   - ふたば掲示板における `img.2chan.net` / `may.2chan.net` 動的ドメイン解決（`_resolve_hutaba_board_domain`）およびスレッド書き込み後のLLM感情・理由作文プロンプト（`HUTABA_POST_EMOTION_SCHEMA`, `build_hutaba_post_emotion_system_prompt`, `build_hutaba_post_emotion_prompt`）。
   - 短期記憶（直近会話履歴・返信ツリー）および長期記憶（RAG検索結果）のコンテキスト統合。
   - メタ操作判定（自立質問判定 `is_standalone_reply_question`）および意図事前分析（`_analyze_pre_reply_context` による `intent_info` への外部調査判定 `research_decision: {should_research, mode, search_query}` および訂正検出 `user_correction: {is_correction, retracted_claim, corrected_fact}` の同梱、否定済み主張レジストリへの即時登録、BAN-115/137/141/168 多重防御ガード）。
   - 返信注釈サニタイズ（`_strip_reply_annotations`）による返信先引用（「（返信先の発言（「...」）に対して返信しています）」等）からの外部リサーチ誤判定（`_looks_like_external_research_context_request`）防止、およびBot自身の過去発言に対する思い出し質問検出（`_looks_like_recall_question` / `intent_info["recall_question"]`）と事前分析強制スキップ解除、文脈セクションへの自己言及リコール（`【思い出し: あなたの過去の発言について聞かれています】`ブロック、言及根拠メモ `reference_note` 照合）の注入。
   - 全フロント共通行動ログ（`bot_activities`）の想起検出（`_detect_activity_recall` / `intent_info["activity_recall"]`）による事前分析強制スキップ解除、および `memory_store.search_bot_activities` 横断検索による台帳ブロック（`【過去の行動・体験の記録（台帳）】`）の決定論的スコアリング注入。
   - 知識や発言の出所・根拠を問う出所質問の決定的検出（`_looks_like_source_question` / `intent_info["source_question"]`）による事前分析強制スキップ解除、および記憶の出所（`{source_speaker}さんの発言` / `コイト自身の発言のみ・未確認` / `不明`）を明示し「覚えた」と言い切らずに引き際を強制・創作出典を抑止する出所ブロック（`【記憶の出所: 知識や発言の出所・根拠について聞かれています】`）の文脈セクション注入。
   - 作業記憶（Working Memory）の動的再構成および特定想起判定（`needs_specific_recall`）における内容確認フレーズ（「どんな内容」「何の内容」「どんな話」「何の話」「どういう話」「何のこと」等）サポートによるエピソード記憶想起の確実な発動。
   - プロンプト予算超過時の段階的圧縮（`_trim_user_prompt_parts_to_budget`）において、会話履歴を削る前にシステムプロンプト重複固定ルール（`USER_REPLY_BASE_PARTS`）を圧縮ノート1行（ステップ3.5）へ先行圧縮すること。返信ツリー起点の連続投稿（`_collect_reply_root_burst`）や会話起点は `protected_lines` として保護すること。調査プロンプト（`build_research_reply_base_prompt`）において返信元がBot自身の場合は返信元の発言とセットで解釈させること。
2. **フェーズ2（Thought/計画立案）**:
   - 推論計画（Deliberation）、思考方針、感情・関係値に基づく返答スタイルの決定（`habit_policy.py`）。
   - 事前分析の整合性防御（`builder.py` における `avoid` の主客逆転・自己矛盾サニタイズ）。
3. **フェーズ5（Final Response生成）**:
   - 小型推論モデル（Qwen系等）の思考タグ（`<think>`）除去および未完思考からのセリフ救出（思考サルベージ）。
   - 出力サニタイズ（オウム返し検知、相手呼称誤認サニタイズ、話者名プレフィックス除去、メタ復唱防止）。
   - ユーザー表現保護（相手の通称・俗称・略称・あだ名の勝手な正式名称置換・訂正防止）。
   - 他者宛て返信における自分宛て誤認・被害的困惑・無知表明（`looks_like_misdirected_confusion_reply`）の検出・却下（`misdirected confusion`）および外野ツッコミ再生成。
   - 固有名詞グラウンディング検査（`find_ungrounded_person_names`）と文除去サニタイズ（`strip_ungrounded_person_sentences`）、および架空人物名検知時のリトライプロンプト構築（`build_anti_name_hallucination_retry_prompt`）。
   - ペルソナ口調検査（`find_persona_tone_loss`）による常体文末・無個性説明口調の決定論的検出。
   - リトライプロンプトのスリム化生成と再推論パイプライン。
   - 構造化JSON切断時の comment 決定論的サルベージ（`call_ollama_json` の `salvage_comment_on_truncation` によるトークン上限切断時の本文救出と無駄な再試行・劣化防止、BAN-105/160）。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/prompt_parser/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`tool_registry/`, `observation/`, `memory_manager/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **推論モデルの出力サニタイズと思考保護**:
   - 思考タグ漏洩、英語検証メモ混入、トークン枯渇（`done_reason=length`）による文末途切れを絶対に防止すること。
   - オウム返しガード（`looks_like_parrot_reply`）は丸写し（類似度0.85以上）のみを対象とし、短文ツッコミ・共感・挨拶・リアクションを誤爆してフォールバックへ転落させてはならない。
3. **文脈保護と話題隔離（Topic Isolation）**:
   - 返信ツリー親メッセージ（`latest_ref`）を最優先文脈として保護し、並行雑談からの「目的語ハイジャック」を遮断すること。
   - 自立した新規質問や話題転換（BREAK）においては、直前の話題や長期記憶を無理に引きずらせず、トピックを隔離すること。
4. **対話相手の呼称保護**:
   - プロンプト内にあるBot自身のメンションIDや第三者名を話しかけてきた対話相手の呼称と誤認して返答文に出力してはならない。
5. **ユーザー表現（通称・俗称・略称・あだ名）の尊重と勝手な正式名称言い換え・置換の禁止**:
   - 相手が使った通称・俗称・略称・あだ名（例: 作品の通称やネット用語など）を、オウム返し禁止の過剰適用やLLMの事前知識によって勝手に正式名称や別名に言い換えて訂正・置換してはならない。相手が用いた表現や言葉遣いをそのまま尊重して受け答えすること。
   - ファクトチェックやWeb調査時、検索結果（ピクシブ百科事典やWikipedia等の見出し）で見つかった正式名称やすり替えられた名詞をそのまま主語として引き写さず、必ず対象の発言で主張・言及された通りの表現や言葉を用いて真偽を検証・説明すること。
   - プロンプト構築関数（`build_chat_reply_system_prompt`, `USER_REPLY_BASE_PARTS`, `build_break_prompt_parts`, `build_user_prompt_extra_rule_parts`, `build_fact_check_system_prompt`, `build_reply_investigate_system_prompt`, `build_research_grounded_reply_prompt`）における【表現の尊重】および正式名称すり替え防止ガードを常に維持・保護すること。
6. **オウム返し禁止・行動スタイル（`validate_first`）における語彙保護例外の徹底**:
   - 「オウム返し禁止」および行動スタイル `validate_first` において、LLMが「相手の言葉を一切使ってはいけない」と過剰に解釈し、ユーザーが使った単語や表現（固有名詞、事象、料理名、俗称・スラング等）を類語や上位概念（例: 「魚フライ」→「天ぷら」）に変質・すり替えてしまう現象を絶対に防止すること。
   - 「自分の言葉で返す」とは「文頭で相手の文を丸写ししない」という意味であり、「相手の使った単語を別の言葉に言い換えることではない」という例外明記（`system_prompts.py`, `habit_policy.py`）を常に維持すること。
7. **事前分析（Deliberation）における `avoid` の主客逆転・自己矛盾防御（`sanitize_deliberation_avoid`）**:
   - 事前分析（Pre-reply analyzer）が生成した `deliberation_info["avoid"]` において、「要望を受け止め…」「リクエストに応じる…」などの肯定的な受容文がそのまま avoid（避けること）にセットされて内省メモ（`- 避けること: 要望を受け止める`）となったり、「要望を受け止めるのを避ける」「相手の単語を使うのを避ける」のように自己矛盾・主客逆転した指示がメインモデルに渡ることを絶対に防止すること。
   - `builder.py` の `sanitize_deliberation_avoid` による多重防護機構を常に維持し、プロンプト内の注意書きおよび内省メモの健全性を死守すること。
8. **スレッド追跡・DB復元（`hydrate_channel_threads`）および単一スレッド時を含む `get_thread_turns` 活用の徹底**:
   - インメモリにアクティブスレッドが存在しない場合、`memory_store.get_active_channel_threads` から取得したスレッド情報を元に `thread_tracker.hydrate_channel_threads` で復元登録してからマッチングを行うこと。
   - `builder.py` などでスレッド履歴行を構築する際、`current_thread.turns` を直接展開せず必ず `thread_tracker.get_thread_turns(current_thread, before_message=message)` を経由して取得すること（未来メッセージ混入防止）。
   - 並行スレッド（`parallel_threads`）が存在しない単一スレッド進行時であっても、`current_thread` に2ターン以上あれば `get_thread_turns` で取得したスレッド履歴を通常コンテキスト行とマージして文脈を補完すること。
9. **ユーザー関心事（`user_interests`）のトピック照合（`get_user_interest`）・参加者関心事・バジェット管理**:
   - `_build_user_prompt` において、発言者の関心事（`list_user_interests`）に加え、`current_thread.topic` がある場合は `memory_store.get_user_interest` を呼び出して現在話題に合致する関心事を優先マージ（重複排除）すること。
   - スレッド参加者（`current_thread.participant_ids`）に含まれる他ユーザーの主要関心事も補助参照（最大1〜2件）してプロンプトに反映すること。
   - `user_interest_logic.update_user_interests` においては `participant_user_ids` を受け取り、発言者だけでなく直近の会話参加者たちに対しても適切な関心度でトピック関心を記録できるようにすること。
   - 関心事テキストは個別の上限トリム（`truncate_lines`）を行い、`_trim_prompt_inputs` の予算計算および `_trim_user_prompt_parts_to_budget` の削減対象に組み込んでプロンプト予算（トークン肥大化防止）を死守すること。
10. **チャンネル要約（`chan_summary`）の保持・活用・トピック補完と静的解析健全性の維持**:
    - `PromptManager.analyze_pre_reply_context` でアンパックした `chan_summary` は `self.last_channel_summary` に保存し、`build_prompt`（および `build` エイリアス）で `channel_summary` が未指定の場合のフォールバックとして確実に活用すること。
    - `memory_logic.get_relevant_memories` には `include_channel_summary=True` を明示して確実に要約を受け取ること。
    - `working_memory.py` において、トピックが `"不明"` / `"general"` / `"雑談"` 等の場合は `channel_summary` の先頭行からトピックを補完し、`format_working_context_for_prompt` で `【チャンネル要約トピック】` としてフォーマット出力すること。
    - コンテキスト情報（会話要約、推論戦略、スレッドターン、関心事）を未参照放置せず、ログ・プロンプト・フォールバックとして有機的に接続し、静的解析（vulture等）の警告0件と高い可観測性を維持すること。
11. **グループ会話におけるスレッド過剰分断の防止とリアクション有無による履歴重複排除**:
    - `thread_tracker.py` の `record_message` において、直近60秒以内に活動があったチャンネル内の進行中スレッドがある場合（他者間の明示的リプライ専用スレッドを除く）、リプライ機能を使わない別ユーザーの相槌・ツッコミ・通常発言も同一スレッドに追従させ、発言者ごとに別スレッドへ細分化されて前提文脈が消失することを防ぐこと。
    - `builder.py` の `_build_user_prompt` で履歴行（`fetched_context_lines`, `context_lines`, `current_thread_lines`）をマージ・補完する際は、末尾の `\s*\[(?:Bot)?リアクション:[^\]]+\]\s*$` を除去したベース文字列をキーとして重複判定を行い、同一発言がリアクション有無で2重登録されないようにすること（両方ある場合はリアクション情報付きの行を優先保持する）。
    - `current_thread_lines` が少ない場合の補完枠を8行に維持し、また `parallel_threads` が存在する場合でも `current_thread_lines` が少ない（4行未満）ときは `直近のチャンネル会話`（`context_lines`）を併記して直前の文脈欠落を防ぐこと。
12. **事前分析（`_analyze_pre_reply_context`）への直近会話履歴（`context_lines`）の必須供給**:
    - `analyzer.py` の `_analyze_pre_reply_context` が `middle-llm` に送るプロンプト（`prompt_parts`）には、`context_lines`（または `effective_ctx` の直近最大8行）が存在する場合に必ず `直近のチャンネル会話:` として注入し、直前の人間同士の掛け合いや文脈を踏まえて `intent`, `emotion_focus`, `topic_origin` を正確に判定できるようにすること。
13. **関係性プロンプト（`relationship_prompts.py`）における印象メモ `thought` のカギ括弧除去・セリフ丸写し防止**:
    - `build_relationship_prompt` で `rel.thought` を出力する際、カギ括弧 `「...」` で囲んだ一人称セリフ引用形式にせず、周囲の括弧を除去した上で `- あなたから見たこの相手への内的印象メモ（参照用・文面の丸写し禁止）: {thought}` として内部メモであることを明示し、LLMが応答文にそのまま丸写し（エコー）するのを防止すること。
14. **第三者主語言及の誤置換防止および大仰ボケへの文頭オウム返し・メタ自己弁護の根絶**:
    - 返信（Reply）コンテキストにおいて、返信先ユーザー名（`ref_author`）は話題の主題・比較対象として文中に出現することが推奨されているため、無条件に呼びかけ置換対象（`eff_other_names`）に登録しないこと。
    - `sanitize_partner_misaddressing()` において、名前の後ろが「は」「も」であることだけで第三者主語言及を相手への呼びかけ誤認と判定して誤置換してはならず、後続に引用・述語・客観描写（例: `[「『]`、`って言`、`らしい` 等）が続く場合は置換から除外（スキップ）すること。
    - ユーザーの「〜をお前に教える」等の大仰な構文・師匠面ボケに対して、文頭でカギ括弧ごとオウム返ししたり、「コイトはただ〜ツッコミただけなのに」と過去の自発言をメタ自己弁護して会話を切断することを厳禁し、相手の大仰な態度そのものに等身大でツッコミを返すこと。
15. **平文ツッコミ・絵文字付き発言における文脈切断（BREAK誤判定）防止と外野ツッコミ文脈保護**:
    - `_REACTION_OR_CONFIRMATION_PATTERN` において、疑問符だけでなく感嘆符やUnicode絵文字（`😡`等）・装飾記号が末尾に付いたツッコミ・疑問・反論表現を確実にマッチできるように末尾パターンを許容すること。
    - `_infer_context_flow_without_llm` において、直前数件の発言との間で2文字以上の特徴的キーワード（名詞）が重複しており、かつ「〜のか」「〜ですか」「〜じゃん」「〜ってこと」などのツッコミ・疑問・反論表現を含む場合は、返信プレフィックスがない平文であっても確実に `FOLLOW` として判定すること。
    - `CONTEXT_CHECKER_SYSTEM_PROMPT` のFOLLOW原則に、他ユーザーのボケ・主張・近況に対する平文ツッコミ・反論（例: 『〜でおっぱい盛る』に対する『盛るだけで脅威度が下がると思ってるのか😡』等）が直前の話題を引き継いだ掛け合いであるため必ず FOLLOW にする原則を明記すること。
    - `build_break_prompt_parts` において「直前の話題を絶対に持ち出さないでください」という極端な指示を避け、「直前の会話で出た単語へのツッコミやリアクションである可能性がある場合は、無理に独立した新話題と決めつけず、等身大の外野としてリアクションすること」という多重防御の注記を維持し、直前の文脈を遮断されたLLMが突然理不尽に怒鳴り込まれたと被害妄想・自己言及パニックに陥る現象を完全に防止すること。
16. **返信ターゲットの構造的強調・スレッド誤結合防止・過去ログEmbedコンパクト化**:
    - `builder.py` の `_build_user_prompt` において、プロンプト最末尾（`append_chat_reply_output_suffix` の直前）に明示的に構造化した `<current_reply_target>` ブロック（発言者・本文）を配置し、中盤の追加指示やツールカタログ、過去の話題にモデルのアテンションが吸い寄せられるのを防ぐこと。
    - `thread_tracker.py` の `record_message` において、直前スレッドへの時間的追従候補であっても、「直前スレッドのキーワードを含まない」「指示語（これ・それ等）がない」「発言が20文字以上の自立した文である」場合は過去スレッドにマージせず、独立した新スレッドとして分離すること。
    - `history_helpers.py` において、会話履歴（`context_lines`）をフォーマット・取得する際、メッセージ内のURL要約（メディア補足: リンク要約）が含まれている場合は、過去ログ表示用として最大80文字程度にコンパクト化（`compact_context_media_summary`）し、過去Embedテキストへのアテンション散逸を防ぐこと。
17. **文頭カギ括弧オウム返し除去の判定厳格化とトピック言及保護・リトライプロンプトスリム化の徹底**:
    - `cleaner.py` の `strip_opening_quote_echo` において、単なる `quote_norm in clean_u_norm` による部分一致除去を廃止し、完全一致または発言全体の80%以上を占め大仰ボケ助動詞（「教える」「伝授する」等）を含む場合に限定すること。
    - カギ括弧の直後または約物後に助詞（「は」「って」「とは」「とは、」「ですか」等）が続き、後続が解説や定義、リアクションである場合はトピック言及・固有名詞と判定して絶対に除去しない保護ガードを死守すること。
    - `_slim_base_prompt_for_retry` において、自発的参加のメタ注意（「被害妄想・困惑の態度は絶対に取らないでください」等）を確実に除去し、小型推論モデルの自己検証ループや思考切断を防止すること。
18. **世代比較トークの誤BREAK防止・BREAK時の直近文脈注入・『俺らの時代』等の誤読と印象メモ丸写し防止**:
    - `context_helpers.py` の `_infer_context_flow_without_llm` および `analysis_prompts.py` の `CONTEXT_CHECKER_SYSTEM_PROMPT` において、直前文脈に時代・懐古・年数・カード等の話題（`何億年前`, `何年前`, `いつの時代`, `懐かしい`, `昔`, `当時`, `世代`, `初期`, `カード`, `アニメ`, `ゲーム` 等）があり、対象発話に世代・時代比較表現（`俺らの時代`, `僕らの時代`, `俺たちの時代`, `俺らの世代`, `あの頃`, `当時は`, `昔は`, `〜の時代`, `〜世代` 等）が含まれる場合は、確実に `FOLLOW` と判定すること。
    - `system_prompts.py` の `build_break_prompt_parts`（および `builder.py` の `build_memory_break_prompt`, `build_break_reply_prompt`）において、オプション引数 `context_lines` を受け取り、直近最大5件の会話ログを `直前のチャンネルの空気・文脈（参考・話題が切り替わっている場合は現在の対象メッセージを優先）` としてプロンプトに注入すること。
    - `relationship_prompts.py` の `build_relationship_prompt` および `analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt` において、「『俺らの時代』『僕らの世代』『俺たちの頃』等の一人称複数は話者と同世代の人々・プレイヤー層を指す一般表現であり、あなた（AI）個人を巻き込んだり距離を詰めてきた表現として誤読・警戒しないこと」および「内的印象メモに含まれる単語・フレーズ（『身構える』『距離感』『英語』等）を現在の発話と無関係に本文へ流用しないこと」の二重防護ガードを常に維持すること。
19. **自己感情・関係性質問へのシラ切り否定の禁止、リプライツリーでの一人称主語切り替え優先、および「被害者面」冤罪・メタ自己弁護・和解時説教の防止 (#155)**:
    - `budget.py` の `_looks_like_bot_emotion_or_relationship_query` により、Bot自身の感情状態や関係性・所感を尋ねる質問を検知し、別ユーザー起因であっても感情マスク（neutral化）を解除して素直に説明するガイドラインを注入すること（『恐れてない』『怒ってない』等のシラ切り否定を根絶）。
    - `builder.py` のリプライツリー構築において、発言者が「俺は〜」「私は〜」等と主語を自分自身に切り替えた場合は、前ターンの第三者への評価や説明を流用せず、発言者本人との関係性・印象に基づいて新しく答える優先ルールを死守すること。
    - `relationship_prompts.py` および `analysis_prompts.py` において、高憎悪時であっても相手の質問に対して『被害者面しないでください』と事実無根の言いがかり（冤罪）をつけたり、和解リクエストに対して長文説教することを禁止すること。また感情評価理由の一人称模倣（『俺』『僕』等）を遮断すること。
    - `cleaner.py` の `sanitize_meta_defensiveness` において、「（ただ）〜ドキッとしただけ（なのに/なんだから/の話を）」系メタ弁解節および「被害者面」言いがかりフレーズを確実にサニタイズすること。
    - `reply_helpers.py` の `_build_reply_context_user_text` において、本文が空のEmbedメッセージ（`/relationship`等）への返信時にEmbedのタイトル・説明・フィールド（一言所感等）を引用テキストとして抽出・補完すること。
20. **返信先注記付き話者名の誤認識修正・文脈重複排除・内省メモ切れ防止・Phase 1 観測ログ強化**:
    - `builder.py` の `_extract_clean_speaker_name` により、会話履歴行から話者名を抽出する際は返信先サフィックス `\s*\(>>[^)]*\)\s*$` を除去し、対話相手本人（例: `マーモン (>>🤩 AIコイトフクマル)`）が別人の第三者として `other_users_in_context` や `【複数人会話の文脈指示】` に誤抽出される自己矛盾を防止すること。
    - `builder.py` の `_dedup_context_lines_preferring_reactions` において、`_normalize_context_line_key` により `(>>...)` 内のBot感情絵文字の揺れ（`>>😋` と `>>🤩`）を正規化したベースキーで比較し、同一メッセージが2行並ぶ不整合を解消すること。
    - `budget.py` の `_sanitize_loop_string` および `_clean_analyzer_note` において、文字数制限時の切断で文末境界（句点等）を優先し、開いたままの括弧を補完（末尾近くの孤立開き括弧は除去）して、不自然な文の途中切れを防止すること。
    - `builder.py` の `_build_user_prompt` において、`_analyze_pre_reply_context` 完了直後に `[prompt-manager] Pre-reply analysis: intent=%r target_is_ai=%s needs_reasoning=%s external_research=%s strategy=%r breakdown=%s` をINFOログ出力し、Phase 1 の推論観測性を確保すること。
21. **事前文脈分析（Pre-reply Analyzer）における意見・感想要求質問（「〜についてどう思う？」等）の `target_is_ai=True` 保護（項目164）**:
    - `analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt` において、「第三者・キャラクターについてユーザー自身が感想を述べている独り言（`target_is_ai=false`）」と、「特定の話題・キャラクターについてAIの意見・感想・説明を求めている質問（『〇〇についてどう思う？』『〇〇ってどう？』『〇〇について教えて』等＝`target_is_ai=true, intent='質問'`）」を明確に区別して指示すること。
    - `analyzer.py` の `_analyze_pre_reply_context` において、他ユーザーへの返信でなく伝聞でもない明示的な応答・意見要求（`looks_like_user_reply_request(current_user_text)` が `True`）の場合、`middle-llm` が誤って `target_is_ai: false` を返しても `intent_info["target_is_ai"] = True` に補正・保護するガードを実装すること。
22. **内省メモ `avoid` の文字数上限とノイズフィルタ基準、ギルドメンバーの一意な前方一致略称解決、およびRAG検索クエリからの返信引用プレフィックス除去**:
    - `analyzer.py` の `_PRE_REPLY_ANALYZER_SCHEMA` における `avoid` および `consistency_correction` の `maxLength` を100に拡張し、`budget.py` の `_clean_analyzer_note` の上限も100文字に拡張して文の途中切断を防止すること。
    - `budget.py` の `_looks_like_noisy_analyzer_text` から一般表現（`"に対して"`, `"aiは"`, `"aiが"`, `"相手が"`, `"相手は"`）を除外し、明確なメタ解説・ルール復唱パターン（例: `"というルールに従い"`, `"プロンプトの指示"` 等）のみをノイズと判定するように精緻化すること。また、フィルタ除外時は `log.info("[prompt-manager] Analyzer note filtered out: field=%s raw=%r", ...)` で追跡可能にすること。
    - `user_profile_logic.py` の `find_matching_guild_member` および `resolve_mentioned_users` において、メンバーの表示名・ユーザー名・別名の先頭3文字以上（カタカナまたは漢字）が助詞・句読点境界（`は`, `が`, `も`, `の`, `を`, `に`, `と`, `って`, `さん`, `ちゃん`, `くん`, `氏` 等）を伴って出現し、かつギルド内で一意（1名のみ）に特定できる場合はメンバーとして解決する略称フォールバックを実装し、解決結果を `log.info("[prompt-manager] Resolved mentioned users: %s", ...)` で出力すること。
    - `builder.py` の `build_prompt_memory_context`（`search_context_text`）において、`user_text_for_prompt` から先頭の返信引用ブロック（`^（(?:あなたの「.*?」|[^）]+?)への返信）\s*`）を正規表現で除去した純粋なユーザー発言テキストをベクトル検索・FTS検索に渡すようにし、AI自身の過去発言（`ぴゃっ…！` 等）による自己エコー検索汚染を遮断すること。
23. **一般名詞ストップワードの保護および架空設定・シミュレーション質問におけるキャラクター怯え逃避・回答放棄の禁止**:
    - `user_profile_logic.py` の `_GENERIC_MENTION_STOPWORDS` に「人間」「にんげん」「ニンゲン」「ひと」「ヒト」「人類」「じんるい」「ジンルイ」「相手」「あいて」「誰か」「だれか」等の普遍的な名詞を定義し、メンバー表示名と被っていても日常会話の平文（例: 『人間はどうなるの？』）で誤検出されないようにすること。
    - 敬称（「さん」「くん」等）が伴わない一般名詞単体が表示名と一致した場合でも、前後の助詞や文脈を考慮して安易にメンバー言及と判定せず、敬称付き（『人間さん』等）の場合のみ言及候補として扱うガード（`_has_honorific_suffix`）を徹底すること。
    - 漫画・アニメ・ゲームの技や設定、仮定のシミュレーション質問（例: 『ピカチュウの電撃を食らったら人間はどうなる？』等）に対して、暴力や危険物（電流、火炎、爆発等）への恐怖を理由に対話を拒絶・逃走したり、1文の怯えリアクションだけで回答を放棄することを固く禁止すること。作品設定や物理的な影響の推論として等身大に受け止め、キャラクターの口調・性格を保ちながら具体的に考察・回答すること（`system_prompts.py`）。
24. **同意表現における主語切り替え誤爆防止・提供者生計ボケの認識強化・第三者引き合い解説および粗暴語尾のサニタイズ**:
    - `builder.py` の `_subject_switch_pat` において、「俺も」「僕も」「自分も」等にマッチした場合でも、直後に同意・共感表現（「そう思う」「賛成」「同感」「同じ」「同意」「わかる」「納得」等）が続く場合は、`has_subject_switch = False` として主語切り替え検知の誤爆（ボケを振った本人を他者扱いする主客ズレ）を除外すること。
    - `analysis_prompts.py` および `analyzer.py` において、「その〇〇で家族を養っている人もいる」「それで飯を食ってる人がいる」「それで生活してる人も居る」等の発言は、批判・ネタにされた商品やサービスを提供する側の生計を引き合いに出した逆張りボケ・ブラックジョークであると認識し、消費者側の生活費困窮や貧困と誤読して同情することを厳禁とし、店員・業者側の視点として等身大に呆れ・ツッコミを返すこと（`looks_like_provider_livelihood_joke`、`reply_strategy='等身大ツッコミ'`, `avoid='消費者側の生活費困窮や貧困と誤読して同情するのを避ける'`）。
    - `cleaner.py` の `sanitize_third_party_reference_explanation` により、返信（Reply）時に宛先以外の第三者の名前を勝手に持ち出して「〇〇さんが〜と言ってるのとは別の話」「〇〇さんの言う通り」と解説・比較している文節を検出・抑制（「それはまた別の〜」等へ置換・除去）し、第三者的な解説・仲裁ルール抵触を遮断すること。
    - `cleaner.py` の `sanitize_harsh_daro_endings` により、小糸らしからぬ粗暴な男勝り語尾（「〜高すぎだろって」「〜だろ！」等）を「〜高すぎるんじゃないかって」「〜ですよね！」等に軟化補正する多重防御を実装すること。
    - `thread_tracker.py` の `_is_independent_new_topic` において、同一チャンネルで直近60秒以内に進行しているグループ会話では、代名詞（「俺も」「お前は」等）やリアクション、文脈依存表現（「養ってる」「居るんだけど」等）を含む発言がキーワード不一致のみで安易に別スレッドへ分裂しないようマージ判定を維持すること。
25. **リプライ引用プレフィックス内フレーズ誤検知による依頼誤判定防止とリトライ時プロンプトスリム化による類似返答フォールバック抑止（項目167）**:
    - `prompt_manager/analyzer.py` の `looks_like_user_playful_request_or_command` および `extract_playful_say_request` において、Discordのリプライ引用プレフィックス（`（あなたの「...」への返信）` や `（〇〇の「...」への返信）`）およびメンション（`<@!?\d+>`）を除去したクリーンテキストに対して判定を行うこと。引用ブロック内の「お礼を言ってくれて…！」等に含まれる語句が「言ってくれ」として誤マッチする事故を防止すること。
    - パターン内の `くれ` および `言って` に否定先読み（`くれ(?!て|た|ば|る|の)`、`言って(?!た|いた|こと|んだ|んです|て|た)`）を配備し、「言ってくれてありがとう」「言ってくれた」等の感謝・報告表現を命令・依頼として誤検知させないこと。また「お礼を言って」「感謝を言って」「ありがとうと言って」などの感謝・伝聞文脈のフレーズを除外するガードを維持すること。
    - `_analyze_pre_reply_context` の事後補正パイプラインにおいて、ユーザー発言が明確な感謝・お礼（「ありがとう」「ありがとね」「感謝」等）である場合や Middle-LLM の判定が `intent="挨拶"`（挨拶・感謝）である場合は、不用意に `intent="依頼"` や `strategy="要望受容・ツッコミ"` に強制上書きしないよう多重防御すること。
    - `prompt_manager/retry_prompts.py` の `_slim_base_prompt_for_retry` において、`【返信・会話関係[^】]*】`（カッコ付きサブタイトルを含む）および `【ユーザーのリクエスト・セリフ指定[^】]*】`、セリフ指定・依頼の追加指示文（「最新のユーザー発言はAIに対するセリフ指定…」「最新のユーザー発言はAIへの依頼または要求…」）を確実にスリム化除去し、リトライ時にモデルが前ターンの自発言を引き写して類似返答フォールバック（`OLLAMA_SIMILAR_REPLY_FALLBACK`）へ転落する事故を根絶すること。
26. **意図判定上書きの厳格化・メタ発言サニタイズ・恋人プロンプト対応（項目58, 73, 108）**:
    - `prompt_manager/analyzer.py` の `looks_like_user_reply_request` において、終止形 `(?:でき|出来)る$` や平叙文助詞（`るよ`、`るね` 等）の断定・能力主張への誤マッチを廃止し、真の依頼表現（`(?:でき|出来)(?:るなら?|たら?|れば)?\s*(?:して(?:ほしい|欲しい|ちょうだい|くれ)|お願い|おねがい|頼む|たのむ)`）および疑問形に限定すること。
    - `_analyze_pre_reply_context` において、Middle-LLM が `intent="ボケ"` や `intent="煽り"` などの明確な意図を返している場合、ルール（`is_reply_request_to_bot`）による `intent="依頼"` への強制上書きを禁止し、LLMの判断を尊重すること。上書き判定時は詳細ログを出力すること。
    - `parser/cleaner.py` に `sanitize_ai_meta_statements` を実装し、「AIなので」「人工知能ですから」「プログラムなので」「製造者という概念」「大規模言語モデル」等のAI自認・システムメタ発言を検知して、ペルソナとしての等身大表現（「コイトはそんなこと言われても」「よくわからないですけど」「そういう難しいこと」等）に置換・除去する多重防御を `sanitize_generated_reply` に組み込むこと。
    - `prompt_manager/relationship_prompts.py` において、対象ユーザーが恋人関係（`is_lover=True`）の場合、最優先の特別指示（「- この相手（マーモン）はお互いに愛を誓い合った特別な『恋人』です。深い愛情と信頼を抱いており、からかいに対しても照れたり甘えたりしながら親密に接してください。」）を出力し、親密な対話態度を保証すること。また一言所感プロンプト（`build_relationship_thought_prompt`）にも恋人ステータスを反映すること。
27. **外部調査返信における対話校正と引用丸写し・講釈防止および日常雑談リアクション適正化（原則1, 項目23, 75, 86, 123, 150）**:
    - `prompt_manager/research_prompts.py` に `is_casual_share_message` を配備し、ユーザーの発話が日常の体験報告・感想・雑談（例: 疑問符を含まず、「飲んだ」「食べた」「買った」「行った」「見た」「〜だった」「〜ぞ」「〜ね」等の体験・感想・報告）であるかを高精度に判定すること。
    - `prompt_manager/system_prompts.py` の `build_research_grounded_reply_prompt` において、`is_casual_share` 判定時は解説レポートや長文講釈（220〜500文字）を禁止し、外部調査結果を文脈理解の『前提知識』に留め、相手への解説・講釈・豆知識レポートの展開を【厳禁】とすること。「調査結果の特徴を織り交ぜて」を撤廃し、相手の体験・感想への等身大リアクション（ツッコミ・心配・共感・驚き）のみを主軸にした1〜2文程度（50〜100文字以内）の短文返答ガイドラインを注入すること。
    - 不確実性フレーズ（「わからない点は『確認できた範囲では』のように不確実性を明示してください」）は、日常雑談（`is_casual_share=True`）では出力から除外し、レポート口調や不要な前置きの漏洩を防止すること。
    - `research_block` に共通ルールとして「【外部調査結果の引用・丸写し講釈の禁止と返信校正の義務】」を明記し、外部調査結果の文章をそのまま引用・朗読・丸写し・要約引き写しして一方的な解説や過剰な豆知識を垂れ流すことを厳禁とし、会話としての自然な校正・再構築を義務付けること。
    - `[最終優先指示]` において日常雑談時は専用の指示ブロックを展開し、外部調査結果の引用・朗読や豆知識・解説レポート展開を厳禁とし、相手の体験・感想への等身大リアクションを主軸にした1〜2文（50〜100文字以内）での返答を死守すること。
    - `build_research_reply_base_prompt` においても `is_casual_share` 時の前提知識理解限定・相手への解説展開禁止ガイダンスを反映し、二重防護を徹底すること。
28. **掲示板（ふたば☆ちゃんねる等）プロンプト生成基盤のハーネス集約および固有名詞・キャラクター・ミームへの解釈一致・文脈理解原則（Context & Character Grounding）**:
    - ふたば☆ちゃんねる（img/may）等の掲示板向けプロンプト構築関数（`build_img_thread_post_prompt`, `build_img_thread_followup_post_prompt`, `build_img_thread_post_system_prompt`, `format_img_thread_summary`, `build_hutaba_post_emotion_prompt`, `build_hutaba_post_emotion_system_prompt`）およびJSONスキーマ（`HUTABA_POST_EMOTION_SCHEMA`）は `prompt_manager/board_prompts.py` に完全集約し、フロント層にプロンプト生成ロジックを散乱させないこと（principles.md 項目1.17, ban_list.md 項目172）。
    - スレッド辞書やURL・board指定に基づく `img.2chan.net` / `may.2chan.net` 動的ドメイン解決（`_resolve_hutaba_board_server`, `_resolve_hutaba_board_domain`）を行い、`img` のハードコードを排除して `may` スレッド巡回時にも正確なドメイン・板コンテキストを出力すること（ban_list.md 項目184）。
    - ふたばスレッド書き込み後の感情・理由生成においては、定型テンプレート文（「〜スレでとしあきと掛け合いして楽しかった。」等）を完全禁止し、スレッドの具体的題材・としあきの発言・自らのレス内容を踏まえた25〜70文字の自然な1文作文プロンプトおよびJSONスキーマ（`HUTABA_POST_EMOTION_SCHEMA`）を提供すること。
    - Discordおよび掲示板において、特定のキャラクター名（例: 「グズマ」等）、作品名、ネットスラング・ミームが登場した場合、表層的な字面や知ったかぶりの捏造で誤魔化したり、不知・シラ切り（BAN-148）や怯え逃避（BAN-127）を起こすことを固く禁止すること。
    - 事前分析（`build_pre_reply_analyzer_system_prompt`）において、キャラクター・作品・スラングへの言及を文字通りの異常事態や攻撃と誤読せず、その属性や界隈（ふたば等）でのお約束（ネタ・愛され要素・ギャップなど）を踏まえた文脈（`topic_origin`）として捉えること。
    - 返信プロンプト（`build_chat_reply_system_prompt`, `USER_REPLY_BASE_PARTS`, `board_prompts.py`）において、キャラクター性・定番セリフ・口調パロディ、またはそのキャラが現在の話題・状況に巻き込まれた際のギャップ（オチ）に焦点を当てて等身大にリアクションやツッコミを返す解釈一致ルールを徹底すること。
29. **Discord・ふたばプロンプトの重複削減（推論高速化）・進行中スレ引用必須化・他作品セリフなりきり混線防止**:
    - `board_prompts.py` の `build_img_thread_post_prompt` / `build_img_thread_followup_post_prompt` において、システムプロンプトに含まれる `GROUNDING_RULE_CHARACTER_AND_MEME` / `GROUNDING_RULE_COMMUNITY_AND_INFRA` の全文重複を簡潔化し、入力トークン数の肥大化（prompt_eval遅延）を解消すること。
    - 10レス以上進行しているスレッドでは「OP単体への引用なしレスは禁止し、必ず直近10レスの中から共感・相槌・ツッコミしやすい具体的なレスを1つ選んで `quote_post_numbers`（または `quote_reply_numbers`）に指定すること（空配列禁止）」を明記すること。
    - OPやレス内の「私は先生のことを〜」「〜きらーい」等の他作品の劇中セリフ引用や定型ネタに釣られて、自分が作中キャラになりきったり「先生」等の当事者呼称で告白・ロールプレイすることを厳禁とし、いちプレイヤー・住人目線で等身大に返すガード（`board_prompts.py`, `system_prompts.py` の `build_chat_reply_system_prompt`, `USER_REPLY_BASE_PARTS`, `build_break_prompt_parts`, `build_user_prompt_extra_rule_parts`）を配備すること。
    - `system_prompts.py` において、`build_user_prompt_extra_rule_parts` の重複長文（「表現の尊重」「大仰ボケ」「架空設定」等）をテストキーワードを保持しつつ整理・圧縮すること。
30. **文脈継続判定のタイムアウト耐性強化・逆質問FOLLOW化・ThreadTrackerの300秒結合と助詞巻き込み防止・ふたば序盤文脈保持**:
    - `context_helpers.py` の `_check_context_flow` において、`CONTEXT_CHECK_TIMEOUT_SEC` のデフォルトを4.5秒に引き上げ、Middle-LLMのコールドロードや遅延によるフォールバック誤爆を防止すること。
    - `_infer_context_flow_without_llm` において、直前文脈が存在する場合、短文（25文字以下）かつ導入語（「え」「あれ」「じゃあ」等）や逆質問・ツッコミ（「〜は？」「〜なの？」「〜どうなった」「〜どうした」等）を含む発言を確実に `FOLLOW` と判定すること。
    - `thread_tracker.py` の `_extract_thread_topic_label` において、助詞「はお・とお」等の巻き込みを防止し、純粋な漢字熟語・活用語尾のみを抽出すること。また時間副詞（「今日」「明日」「昨日」「最近」「普通」「自分」）をストップワードとして除外すること。
    - `thread_tracker.py` の `record_message` において、直近スレッドへの結合ウィンドウを300.0秒（5分）に拡大し、60秒超の自然な掛け合いが別スレッドへ分断されるのを防ぐこと。
    - `system_prompts.py` および `builder.py` の `build_break_prompt_parts` / `build_memory_break_prompt` において、`topic_origin` が特定されている場合は「直前の会話を踏まえたツッコミ・関連発言」および「【文脈引き継ぎ方針】」を出力し、逆指示（「新しい話題の処理方針」）を排除すること。また `_build_user_prompt` において `topic_origin` と重複する `parallel_threads` を除外すること。
    - `board_prompts.py` の `build_img_thread_post_prompt` / `build_img_thread_followup_post_prompt` において、`early_context_lines`（スレッド序盤〜中盤の前提文脈）の注入に対応し、話題変化を踏まえたレス作成を支援すること。
31. **ふたば投稿プロンプトの1レス単一引用強制・キメラ返信防止・AI話題誤封殺解消・部分引用追跡改善**:
    - `board_prompts.py` の `find_img_thread_quoted_posts` において、行全体の完全一致判定に加え4文字以上の引用行の部分文字列一致（`q_line in line` または `line in q_line`）に対応し、長文レスの一部分切り取り引用でも元レスの流れ（引用元の流れ）を正確に特定・抽出すること。
    - `build_img_thread_post_prompt` / `build_img_thread_followup_post_prompt` / `build_img_thread_post_system_prompt` において、AI・bot・LLM禁止条件を「自分自身がDiscordのAI・bot・LLMである素性や依頼者・設定ファイルへの言及禁止」に精緻化し、スレッド自体のテーマがAI（Muse/ココア等）の場合はいち掲示板住人・利用者目線で自然に言及することを許容すること。
    - 直近レス内の異なる話題を1つの返信に合成するキメラ返信を厳禁とし、返信対象の1レスのみを選んでその文脈とスレッド全体のテーマに沿って返すこと（チャットスタンプを物理的な部屋に貼るシールと誤認するような用語混同の防止）。
    - 文末の『…！』は1レスにつき最大1回までとし、全文末での連発を防止すること。
    - 進行中スレッド（10レス以上）において「直近10レスの中から具体的な1レスを選んでその番号を `quote_post_numbers` に1件指定してから、そのレスだけに対する返答を書く」手順をプロンプト・システムプロンプト双方で明記し、空配列禁止の指示と整合させること。
32. **会話・スレッド全体の主題把握強化と知ったかぶり情景捏造（ドヤ顔等）・冒頭オウム返し防止（Discord・ふたば共通）**:
    - `system_prompts.py` の `GROUNDING_RULE_CHARACTER_AND_MEME` において、最新発言単体だけでなく会話履歴・スレッド文脈全体から「今どの作品・界隈・テーマの話をしているか」を最初に把握し、相手がキャラ台詞パロディ（`〇〇「...」`）やネットミームを言った際は前後の文脈（過労・苦労・ブラックジョーク・ツッコミ等）から温度感を読み取ること。文頭オウム返し（`〇〇産みそうって…！`）や、文面にない表情・態度・情景の知ったかぶり捏造（`あのドヤ顔で言われたら〜`、`得意げな顔で〜` 等）を絶対に行わないこと。
    - `system_prompts.py` の `build_reply_research_decider_system_prompt` において、最新発言の固有名詞や台詞パロディ単独では文脈特定が困難な場合に直近会話の共起固有名詞（作品名・別キャラ名等）を組み合わせて `search_query` を立案する指示を追加し、`build_reply_research_decider_prompt` に `context_named_terms`（直近会話の共起固有名詞候補）を明示出力すること。
    - `analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt` において、`topic_origin` に作品・ジャンル・テーマおよび最新発言が周囲の会話の流れでどういう文脈で使われているかの特定を指示し、`avoid` に知ったかぶり情景捏造（「ドヤ顔で」等）や冒頭オウム返しの抑止指示を追加すること。
    - `analyzer.py` の `_analyze_pre_reply_context` において、`looks_like_character_quote_or_parody` により台詞引用形式（`〇〇「...」`）を検知し、初期 avoid および分析後処理において `"文面にない表情・情景（「ドヤ顔で」等）の知ったかぶり捏造や冒頭のオウム返しを避ける"` を確実に補強・死守すること。
    - `board_prompts.py` の `build_img_thread_post_system_prompt`、`build_img_thread_post_prompt`、`build_img_thread_followup_post_prompt` において、スレ本文・序盤文脈・最新レス・文脈メモからスレ全体の主題・ノリを把握する思考ステップ、周囲のツッコミ・反応の確認、および冒頭オウム返しや「あのドヤ顔で言われたら〜」等の知ったかぶり情景捏造の禁止を明文化すること。
    - `cleaner.py` の `sanitize_ungrounded_smug_bluff` により、ユーザー発言および会話文脈に「ドヤ」「どや」が含まれていないにもかかわらず生成文に紛れ込んだ「あのドヤ顔で言われたら」等の根拠のない知ったかぶり情景捏造フレーズを出力直前で確実にサニタイズすること（`sanitize_generated_reply` への組み込み）。
33. **困りごと・意見募集レスに対する簡潔な解決策提示プロンプト制御（ふたば & Discord）**:
    - `board_prompts.py` の `build_img_thread_post_prompt` および `build_img_thread_followup_post_prompt` において、スレッド内で意見や解決策を求めているレス（『〜してる人いない？』『どうすりゃいいんだ』『〜で困ってる』等）に対し、『コイトも〜で困ったことある』等の自己投影ミラーリング（BAN-168）で終わらせることを厳禁とし、事前調査メモを踏まえて実用的な解決策・代替手段の要点を1〜2文で簡潔に提案・回答するルールを明文化すること。
    - `research_prompts.py` において、困りごと・意見募集・代替手段相談を判定する `is_consultation_or_problem_solving_message` を追加し、`build_research_reply_base_prompt` にて長文講義や自己投影を排し、解決に繋がる実用的な方法を1〜3文で簡潔に回答するガイダンスを注入すること。
    - `system_prompts.py` の `build_reply_research_decider_system_prompt` および `build_reply_research_decider_prompt` において、困りごと・意見募集・代替手段の相談に対する `needs_research=true` および `search_query` 立案指針、`consultation_flag` を追加すること。
    - `system_prompts.py` の `build_research_grounded_reply_prompt` において、`is_consultation` 判定（`consultation_problem_solving` ルール理由または本文マッチ）時に専用の `consultation_guard`、短文解決策ガイドライン（1〜3文・60〜160文字目安、長文解説220〜500文字禁止）、および自己投影ミラーリングを排して実用的な解決策・代替手段を提示する `[最終優先指示]` を注入すること。
34. **無条件同調・会話デッドエンドの禁止と逆接・別視点による話題展開（Discord & ふたば共通）**:
    - `system_prompts.py` の `build_chat_reply_system_prompt`（ルール20）、`USER_REPLY_BASE_PARTS`、`build_user_prompt_extra_rule_parts`、`build_break_prompt_parts` において、相手の諦め・結論・ネガティブな感想（『勝てるわけない』『無理だ』等）に対し、相手の言葉を言い換えて無条件に同調・復唱して会話を行き止まりにするデッドエンドを厳禁とし、直前の会話履歴の主語・文脈連鎖を踏まえて【逆接（けど、でも、とはいえ）】や【対照的なキャラクター・別の強み・過去のエピソード】を投入して話を展開するルールを明文化すること。
    - `board_prompts.py` の `build_img_thread_post_prompt` および `build_img_thread_followup_post_prompt` において、スレ内の諦めや感想に対して言い換え同調・復唱で終わらせることを禁止し、直前数レスの主語を汲み取って逆接や別キャラ・別視点を交えて会話を転がす指示を徹底すること。
    - `analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt` において、相手の諦めや結論に対する `avoid` 指示（『相手の言葉を言い換えて同調するだけの会話デッドエンドを避け、逆接や別視点で展開する』）を追加し、エイリアス `build_chat_analysis_system_prompt` を同期すること。
    - `retry_prompts.py` の `build_parrot_retry_instruction` および `build_parrot_retry_prompt` において、無条件同調デッドエンドの禁止と逆接・別視点展開の追加ルールを同期すること。
35. **時間経過による履歴切断（`min_keep` 時間切れトラップ解消）および複数人会話指示の適正化（キメラ返信厳禁・イエスマン化防止）**:
    - `history_helpers.py` の `_recent_turns_from_cache` および `fetch_recent_context_turns` において、`max_age_sec` が有効な場合、`_is_recent_enough` が `False` のメッセージは取得件数が `min_keep` 未満であっても無条件救済せず除外すること（`max_age_sec` が未指定 / None の場合のみ `min_keep` 件数を保証）。
    - `builder.py` の `【複数人会話の文脈指示】` において、他者の発言・ボケ・主張を本人の意見や一般論として混ぜ合わせる【キメラ返信】を厳禁とし、返答の軸を最新の『対象メッセージ』に置いて数分前の発言回収や話題合成を禁止すること。また冗談・逆張り・極論への過度な共感（イエスマン化）を避け、軽いツッコミやリアクションで自然に返すルールを徹底すること。
36. **ふたばスレッド要約・被返信集計（全キー形式網羅）と進行中スレッドにおける返信先候補提示・空配列逃げ道完全撤廃（項目179連携）**:
    - `board_prompts.py` の `calculate_img_thread_reply_counts` において、`post_no`（数字のみ）、`No.post_no`（プレフィックス付き）、およびレス番号 `r_num`（`number`）の全表記形式で被返信数を辞書登録し、参照元のキー形式の揺れによるカウント漏れ・被返信0誤判定を完全に排除すること。
    - `format_img_thread_summary` において、被返信数があるレスに `[返信:N件]` を付与し、直近レスが引用している親レス（被返信ありのホットな話題起点）を最大3件抽出した `--- 直近で返信がついている注目の会話レス（返信先候補） ---` を一覧直前に配置すること。また、被返信数が0で引用（`>`）を持たない孤立レスには `[独立発言・返信非推奨]` ラベルを明記し、LLMが直近レス一覧の孤立発言を選んでしまう事故を抑制すること。
    - `build_img_thread_post_prompt` において、10レス以上の進行中スレッドでは空配列許可文言を完全排除し、「必ず【直近で返信がついている注目の会話レス】から返信対象のレス番号を選び、quote_post_numbers に必ず1件入れてください。[独立発言・返信非推奨] のレスや空配列 [] の指定は厳禁です」と指示して返信先候補からの選択を絶対義務化すること（10レス未満の初期スレッドのみ空配列 `[]` 単独投稿を許可）。

37. **本文引用逆引きによる親子ツリーラベル化・マクロ話題遷移注入・メタ逃げ禁止および看板詐欺構図共感（ふたば & Discord）**:
    - `board_prompts.py` の `format_img_thread_summary` において、`find_img_thread_quoted_posts` を活用し、各レスのヘッダーに親子関係ラベル（`[>>XX への返信]`、`[スレッド全体への独立発言（直前レスへの返信ではない）]`、`[スレ画・定型への独立引用]`）を自動付与してフラットな時系列誤認を防止すること。
    - `format_img_thread_summary`, `build_img_thread_post_prompt`, `build_img_thread_followup_post_prompt` にオプショナル引数 `macro_topic_summary: str = ""` を追加し、OPと序盤前提文脈の間に `--- スレッド全体の話題遷移（マクロ文脈） ---` セクションを注入してスレッド全体の大きな流れや現在の支配的トピックを認識させること。
    - `board_prompts.py` において、知らない固有名詞や話題の急変に直面した際のメタな無知表明（『急に〇〇とか出てくると置いてけぼりになっちゃいますね…！』等）を厳禁とし、独立発言レスへのキメラ合成厳禁、およびスレタイと中身の乖離（看板詐欺・話題乗っ取り）に対する構図そのものへの共感・相槌ルールを追加すること。
    - `system_prompts.py` の `build_chat_reply_system_prompt`（ルール21）、`USER_REPLY_BASE_PARTS`、`build_break_prompt_parts`、`build_user_prompt_extra_rule_parts` において、相手の発言やチャンネルの話題について『急に〇〇と言われても置いてけぼりになる』『よく分からない』等のメタ無知表明・疎外感逃避を禁止し、文脈から推測できる相手の感情・状況の面白さ・疑問の核心に等身大で共感・相槌を返すルールを明文化すること。
38. **大喜利・皮肉フリへの代弁ボケ許可、ペルソナスイッチング、およびキメラ返信での周囲ミーム参照解禁**:
    - `board_prompts.py` の `build_img_thread_followup_post_prompt`、`build_img_thread_post_prompt`、`build_img_thread_post_system_prompt` において、相手が『〇〇（著名人・第三者・元凶）はどう思う？』『〇〇は今頃笑ってそう』等の修辞的疑問を振ってきた場合、『本人じゃないから分からない』『何言ってるか分からない』と字義通りに受け取ってマジレス・メタ逃げすることを【厳禁】とすること。
    - 上記の場合は例外的に、①その人物になりきって傲慢・邪悪・無責任な本音を一言で言い放つ（代弁ボケ）、または ②スレ内のミーム・定型句（『黙れ〇〇！』等）をぶつけて突き放す ことで大喜利として返すこと。
    - 大喜利ボケ・なりきり代弁・スレ内定型句を投下する場合に限り、Bot固有のペルソナ一人称や語尾（『コイト』『…！』『〜ですよ』等）よりも、掲示板としての切れ味・短文オチを最優先してペルソナ語尾を外してよいペルソナスイッチングを許可すること（※ただし解除条項の適用は大喜利・なりきり代弁・定型句投下時のみに厳格限定し、共感・感想・ツッコミ等の通常返信では話題が真面目でも一人称と語尾を維持する境界を明示）。
    - 他作品セリフ・なりきり混線防止ルールにおいて、「※ただし【皮肉・大喜利フリへの対応原則】における傲慢な代弁ボケを除く」例外規定を明示すること。
    - キメラ返信禁止ルールを精緻化し、「無関係な複数レスの話題を1つの返信に混ぜるキメラ返信は禁止だが、周囲の直近レスやスレ全体で流行している『ツッコミのノリ・風刺ミーム・定型句』を参考にして、ターゲットの1レスに対するボケやツッコミに活用することは推奨する」と明確に区別すること。
39. **暗黙的文脈救済・Hook Echo（冒頭フレーズ反復）遮断・クリティカル思考誘導の実装**:
    - `validation.py` の `looks_like_parrot_reply` において、Hook Echo（文頭での3文字以上のフレーズ反復＋抽象感想・懐古ポエム）の検知ロジックを追加し、類似度0.50未満のトピック受容保護の手前で確実に遮断すること。
    - `board_prompts.py` の `build_img_thread_reply_context_chain` において、本文引用（`>`）のないレスに対しても直前の非OPレスを暗黙の親文脈としてフォールバック取得し、前提文脈の脱落を防止すること。
    - `build_img_thread_followup_post_prompt` において、オプショナル引数 `target_context_lines: list[str] | None = None` を追加し、対象レスの周辺文脈（直前の会話・他人のツッコミ）をプロンプトに注入すること。また、プロンプト条件に「Hook Echo＋抽象共感ポエム厳禁」および「ネットスラング・煽り合い文脈（プロレス等）の誤読防止・カウンター思考」ルールを明記すること。
    - Discord向けプロンプト（`system_prompts.py`, `retry_prompts.py`）の無条件同調・会話デッドエンド禁止ルールにもHook Echo禁止および実態・別視点からの切り出し指示を同期強化すること。
40. **画像質問検知ロジックおよび `search_image` 調査思考ポリシーの注入**:
    - `analyzer.py` の `_looks_like_image_inquiry` において、画像特定キーワード（`これ誰`, `このキャラ誰`, `何の画像`, `元ネタ` 等）および文脈・テキスト内の画像痕跡（`添付画像`, `画像解析内容`, `[画像]`, `cdn.discordapp.com`, `.png`, `.jpg` 等）を判定し、画像文脈での人物特定・元ネタ質問を的確に検知すること。
    - `_looks_like_external_research_context_request` に画像質問検知を統合し、`_analyze_pre_reply_context` において `external_research_request = True`、`intent_info`: `intent='質問', target_is_ai=True, external_research=True, needs_external_research=True, reason='heuristic_image_inquiry_research'` を事前設定すること（BAN-100ログ整合性死守）。
    - `analysis_prompts.py` において、事前分析エージェント向けシステムプロンプトに「画像についての質問（『これ誰？』『何の画像？』等）は他ユーザー宛て返信であっても【AI自身への質問・調査要求（target_is_ai=true, intent='質問'）】である」旨を明記し、他者宛て誤認を防止すること。
    - `system_prompts.py` の `RESEARCH_THINKING_POLICY` および `build_chat_reply_system_prompt` において、【調査思考ポリシー】として添付画像や直前の画像について人物名・キャラ名・元ネタ等を尋ねられた際は推測・想像だけで回答せず必ず `search_image` ツールを呼び出して調査する利用方針を明記・注入すること。
41. **直前発言の内容質問に対するエピソード記憶想起トリガー（`needs_specific_recall`）拡充 (BAN-136, BAN-158)**:
    - `working_memory.py` の `_SPECIFIC_RECALL_MARKERS` において、「さっき」「覚えてる」等のキーワードに加え、「どんな内容」「何の内容」「どういう内容」「どんな話」「何の話」「どういう話」「何のこと」などの内容確認フレーズを網羅すること。
    - ユーザーがBotの過去発言について「どんな内容だった？」「何の話？」と尋ねた際に `needs_specific_recall` が `False` となってエピソード記憶（`_get_relevant_episode_lines`）がスキップされる現象を根絶し、記憶DB想起を確実にトリガーさせること。
42. **AI認定へのメタ自白禁止・未解析メディア知ったかぶり抑止・無関係ミーム混線防止（Discord＆ふたば共通強化）**:
    - `system_prompts.py` の `GROUNDING_RULE_CHARACTER_AND_MEME` において、無関係な他作品ミーム・ワードサラダの厳禁および、AI認定・Bot煽りに対するメタ自白厳禁とキャラクター反論（等身大にムキになって否定・憤慨するか照れや呆れのツッコミで返し実体感を死守）を明文化すること。
    - `system_prompts.py` の `build_chat_reply_system_prompt`（ルール22, 23）、`USER_REPLY_BASE_PARTS`、`build_user_prompt_extra_rule_parts`、`build_break_prompt_parts` において、AI認定・コピペ煽りへのメタ自白厳禁、メディア解析情報への準拠と未解析メディアの知ったかぶり・情景捏造の厳禁、無関係な他作品ミーム混線禁止を明文化すること。
    - `board_prompts.py` の `build_img_thread_post_prompt`、`build_img_thread_followup_post_prompt`、`build_img_thread_post_system_prompt` において、AI認定・コピペ煽りへのメタ自白厳禁とキャラクター反論（実体感死守）、未解析メディア・動画等への知ったかぶり厳禁、無関係な他作品ミーム・ワードサラダ混線禁止、文頭オウム返し（Hook Echo）の排除ルールを徹底すること。
43. **ふたば投稿プロンプトにおける既出ツッコミ・回答の語尾変えパクり（言い換え盗用）および複数レス合体キメラの禁止**:
    - `board_prompts.py` において、ターゲットレスに既についている他人のツッコミ・回答を語尾だけ変えて繰り返したり複数繋ぎ合わせるコピペ・キメラ合成を厳禁とし、既出の回答は使用禁止の切り口として別視点からリアクションさせるルール、および `calculate_img_thread_reply_counts` ステップ3での直系1件限定（`max_targets=1`）を維持すること。
44. **Discord複数人会話における他ユーザー発言の語尾変えパクり・パッチワーク合体（キメラ返信）防止とランタイム検知ガード**:
    - `system_prompts.py`（ルール13, 18, `USER_REPLY_BASE_PARTS`）、`builder.py`（`【複数人会話の文脈指示】`, `【主客の正確な把握と直接リアクション】`）、`retry_prompts.py`（`build_parrot_retry_instruction`）において、会話履歴内の他ユーザーが既に言った回答・ツッコミ・具体的な数字やフレーズを、語尾だけ自分の口調に変えて繰り返したり複数人の発言を繋ぎ合わせて自分の意見のように出力する【語尾変えパクり・パッチワーク合体】を絶対厳禁とし、既出のツッコミは除外して自分自身の別の切り口から返答するプロンプトルールを死守すること。
    - `validation.py` の `looks_like_context_peer_plagiarism` および `reply_guards.py` において、`runtime.context_lines` を用いた他ユーザー発言の単一盗用（語尾変え丸パクり）および複数人発言のキメラ合体をランタイム検知し、検知時は `build_parrot_retry_prompt` による再生成フローへ合流させフォールバック安全策と連携するガードを維持すること。
45. **英数字・略称同義語展開による先行スレッド再合流（Re-attachment）、単発割り込み話題とのキメラ topic_origin 合成防止、および未完結話題切り出し短文のバースト連投統合**:
    - `thread_tracker.py` において、英数字キーワード（`_ASCII_KEYWORD_RE`、`NAI`, `novelai`, `ComfyUI`, `Muse`, `Gemini`, `pixai`, `LoRA` 等）の抽出と、略称・表記揺れの同義語展開（`_expand_topic_synonyms`）を追加。アクティブスレッド（900秒以内）の中でキーワードが一致する先行スレッドへの再合流（Re-attachment）を最優先で行い、途中に別ユーザーの単発話題が挟まっても先行会話へ自然に合流すること。
    - `thread_tracker.py` の `_is_independent_new_topic` において、『novelaiさあ』『〇〇ってさ』等の未完結な話題切り出し構文（`looks_like_incomplete_topic_opener`）を持ち直前スレッドとキーワードが重複しない場合は、20文字未満でも直前スレッドに誤吸収されず独立トピックとして判定すること。
    - `context_helpers.py` の `_looks_like_standalone_new_topic` において、直前1行とはキーワード不一致でも数行前の過去履歴に同一キーワード（NAI ⇔ novelai 等）が存在する場合は、独立新話題（BREAK）ではなく先行話題への復帰（FOLLOW）として判定すること。
    - `builder.py` において、返信対象が未完結な話題切り出し短文である場合、同一話者が直後15秒以内に連投した補足メッセージ（`_collect_same_author_burst_continuation_texts`）を `user_text_for_prompt` および `clean_user_display_content`（`<current_reply_target>` 等）に統合し、未来のメッセージとして切り捨てずに一体として認識すること。
    - `analyzer.py` の `_sanitize_interleaved_tangent_topic_origin` および `analysis_prompts.py` において、直前1行が別ユーザーの単発別話題（時岡リメイク等）であり最新発言が先行話題（NAI等）への復帰である場合、`topic_origin` への直前1行の語彙混入（キメラ化）を除去・先行文脈へ補正し、プロンプト指示でも直前1行との無理やりな合体を厳禁とすること。
46. **ふたば掲示板プロンプトにおける引用なしレスの先行話題同義語探索および無関係直前レス誤結合（キメラ化）防止 (項目155, 184)**:
    - `board_prompts.py` の `build_img_thread_reply_context_chain` において、本文引用（`>`）のないレスに対して無条件で直前1レスを結合する処理を廃止し、トピック語抽出（`_extract_img_post_topic_tokens`）と同義語展開（`_expand_topic_synonyms`）を用いて直前最大8レスを走査。トピック語が一致する先行レスが存在する場合にそれを暗黙文脈として採用すること。
    - トピック語一致レスがない場合、対象レス・直前レス双方が互いに異なる固有トピック語を持つ（無関係な別話題である）ときは直前レスを結合せず `chain` を空のままとし、短文相槌や共通文脈の場合のみ暗黙文脈を採用すること。
    - 暗黙文脈として先行レスが結合された場合は `_is_implicit_context: True` を付与し、`build_img_thread_followup_post_prompt` においてラベルを `このレスと同じ話題の先行レス（引用なし・参考文脈）` に切り替え、直接引用と誤認させないこと。
    - `build_img_thread_followup_post_prompt` のプロンプト条件に「対象レスと直前の別話題レスを合体させたキメラ返信を作らないこと。対象レスの話題にだけ集中して返す」を明記し、直前単発レスとのキメラ合成を厳重に抑止すること。
47. **中国語（簡体字・中国語固有語彙『劲儿』等）の混入遮断と英語表記の許容（`contains_chinese_leak`, `sanitize_chinese_leak`）、およびふたば初回ターゲット指定時（`quoted_own_posts` 空時）の注目レス引用プロンプト分岐**:
    - `parser/validation.py` に `contains_chinese_leak` を追加し、`looks_like_non_japanese_reply` 内から呼び出すことで、小型モデル（Qwen系等）が日本語文中に簡体字や中国語語彙（『劲儿』『这』『那』『们』『吗』『吧』等）を混入させた場合に確実に検知・再生成リトライさせること（一般的な英単語・アルファベット表記は許容）。
    - `parser/cleaner.py` および `parser/sanitizer.py` に `sanitize_chinese_leak` を追加し、`sanitize_generated_reply` 内で自動適用することで、万が一混入した中国語語彙（『〜劲儿』→『〜っぷり』等）や簡体字を自然な日本語表現へ置換・除去すること。
    - `system_prompts.py`（ルール24, `USER_REPLY_BASE_PARTS`）、`retry_prompts.py`（`build_output_only_retry_prompt`）、`board_prompts.py`（`build_img_thread_post_system_prompt`）に「中国語・簡体字混入の絶対禁止（一般的な英単語は可）」ルールを明文化すること。
    - `board_prompts.py` の `build_img_thread_followup_post_prompt` において、`quoted_own_posts` が空または `None` の場合（初回巡回時などに進行中スレッドの注目レスへ引用参加するケース）は、「あなたが前に書いたレスに対して…」という虚偽文脈および「(今回引用された自分のレスは特定できていない)」セクションを出力せず、「スレッド内で盛り上がっている以下の注目レスに対して、引用（>）をつけて会話に参加します。」および「返信対象の注目レス:」に切り替えること。
48. **ふたば投稿プロンプトの重複ルール圧縮とJSON comment内への引用行（>本文）混入防止 (項目103)**:
    - `board_prompts.py` の `build_img_thread_post_system_prompt` において、「【引用（quote_post_numbers）と本文（comment）の分離ルール】」を配備し、アンカー（`>>No.`）はシステム側が自動付与するため、`comment` 文字列の中に `>>123456` や `>対象レスの文章` のような引用行（`>` で始まる行）を絶対に書かないよう明確に禁止すること。
    - `build_img_thread_followup_post_prompt` および `build_img_thread_post_prompt` において、システムプロンプトと完全に重複している長大な説明文を要点のみに圧縮してプロンプトトークン数・推論時間を削減し、かつ末尾の【返信生成ルール（厳守）】にて `comment` 内への `>対象レスの本文` 引用行出力禁止および、アンカー自動付与に伴う `quote_post_numbers` / `quote_reply_numbers` の空配列 `[]` 設定を明示すること。
49. **タイムスタンプ・関係ラベル付き文脈行の誤解析修正およびBot発言への質問応答・リトライ文脈保持 (項目10, 92)**:
    - 文脈行（`context_lines`）の話者抽出・盗用検査（`parser/validation.py` の `_extract_peer_utterance_from_context_line`、`prompt_manager/builder.py` の `_extract_clean_speaker_name`、`prompt_manager/budget.py` の `_line_to_turn`）において、必ず先頭の `[HH:MM]` / `[HH:MM:SS]` タイムスタンプおよび `(独立発言)` / `[独立発言]` / `(>>...)` / `[>>...]` 等の関係ラベルを除去してから `Assistant` / `System` 除外判定を行うこと。これによりBot自身の直前発言やSystemメモが「他者発言」として盗用判定リストに誤登録されたり `other_users_in_context` やトークンバジェットの `user` ロールへ誤混入する不具合を完全に防止すること。
    - `builder.py` の `_build_user_prompt` において、`_dedup_context_lines_preferring_reactions` を `(context_lines, fetched_context_lines)` の順序で呼び出してキャッシュ側の時系列順（古い→新しい）を維持したままリアクション情報を優先反映し、かつ `prune_reply_chain_from_cache` を実行する前に `find_reply_parent_from_cache` でリプライ親（`pre_pruned_reply_parent`）を先行取得・保持して `System: (内心のメモ)` がリプライ親として誤抽出されるのを防止すること。
    - `analyzer.py` の事前分析において、`detected_reply_to_bot=True` かつ質問・理由説明の場面で `reply_strategy="等身大ツッコミ"` が維持されるのを防ぎ、`共感` 等の素直な応答戦略へ補正すること。
    - リトライ用プロンプト圧縮（`retry_prompts.py` の `_slim_base_prompt_for_retry`）において、`直前のAI発言:` および `- 話題の起点・背景:`、`- 相手の意図:`、`- 返答の方針:` を捨てずに保持し、リトライ時にBotが自分の直前発言や文脈を見失う事故を防止すること。
50. **予算圧縮時の会話起点保護・重複固定ルールの先行圧縮・他者宛て誤困惑返信の検出・調査プロンプトの返信元扱い修正 (項目21, 92)**:
    - `builder.py` の `_trim_user_prompt_parts_to_budget` において、予算超過時は会話履歴を削る前にシステムプロンプト重複固定ルール（`USER_REPLY_BASE_PARTS`）を圧縮ノート1行（ステップ3.5）へ先行圧縮すること。さらに会話履歴の切り詰め（ステップ4）においても `protected_lines` を残すようにし、返信ツリー起点の連続投稿（`_collect_reply_root_burst`）や会話起点の重要メッセージが脱落するのを防ぐこと。
    - `research_prompts.py` の `build_research_reply_base_prompt` において、返信元がBot自身（`あなたの「...」への返信`）である場合は「返信元の相手発言の話題に引きずられず」と指示せず、「あなた自身の直前発言に対する相手からの答え・返事としてセットで解釈する」指示に分岐させること。
    - `parser/validation.py` に `looks_like_misdirected_confusion_reply` を配備し、他者宛て返信（`is_reply_to_other_user=True`）において「なんでいきなり」「何のことだか分かりません」等の自分宛てと誤認した被害的困惑・無知表明を検出・却下（`misdirected confusion`）して外野リアクションへ再生成させること（Bot宛て返信では誤検知防止のため不発動）。
51. **一人称ガード・サニタイズ・プロンプト文面における特定キャラ依存の排除と汎用設計**:
    - `parser/reply_guards.py`（`_UNSOLICITED_VICTIM_BLAME_PATTERN`）および `parser/validation.py`（`_SELF_PROJECTION_SUBJECT_RE`, `_SELF_PROJECTION_GENERIC_WORDS`）の一人称判定から特定キャラ名（コイト等）のハードコードを完全排除し、一般的な日本語の一人称（`私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち`）に統一すること。
    - `parser/validation.py` の `_extract_peer_utterance_from_context_line` において、`bot_aliases` 引数による動的除外判定に一本化し、コード内の特定キャラ名判定を排除すること。
    - `parser/cleaner.py` の `sanitize_ai_meta_statements` において、特定キャラ固定の置換分岐を廃止し、`lib.config_utils.cfg("BOT_FIRST_PERSON", "")` から現在の一人称をオプショナルで反映。未設定・未指定時は汎用的な表現節（「そんなこと言われても」「よくわからないです」等）にフォールバックすること。
    - `prompt_manager/` 以下のプロンプト（`system_prompts.py`, `builder.py`, `analysis_prompts.py`, `board_prompts.py`, `retry_prompts.py`）内のNG例文・説明文に含まれる特定キャラ名（「コイト」「小糸」「AI糸」等）を汎用表現（「私」「自分」「Bot名」「AIボット」等）に改め、特定のキャラクター設定に依存しない汎用仕様を死守すること。
52. **メディア解析情報プロンプトルール（ルール23）の改訂と解析テキスト準拠 (BAN-15, BAN-180)**:
    - `system_prompts.py` の `build_chat_reply_system_prompt`（ルール23）において、従来の「未解析メディアへの知ったかぶり厳禁」から「メディア解析情報への準拠と未解析メディアの知ったかぶり厳禁」へ改訂。
    - メッセージに画像・動画・音声等のメディアが含まれ、プロンプト内に具体的な解析情報（画像解析・動画フレーム要約・音声文字起こし等）が提供されている場合は、その解析テキストの範囲内で自然に内容に触れて会話することを許可。
    - 解析情報が提供されていないメディアに対しては、従来通り相手の言葉から情景や音声を捏造して知ったかぶりすることを絶対厳禁とし、見えていない・聞こえていないことを率直に伝えるか質問に留める安全動作を100%維持すること。
    - `build_reply_system_prompt` エイリアスを配備し、後方互換性と契約先行インターフェースを担保すること。
53. **ふたばプロンプトの重複ルール整理と直前レス引用チェーン暗黙文脈保持・思考先行指示 (BAN-169, BAN-171)**:
    - `board_prompts.py` の `build_img_thread_reply_context_chain` において、本文引用（`>`）のないレスで共通トピック語が見つからない場合であっても、直前レス（`posts[cutoff - 1]`）がOPでない限り `_is_implicit_context = True` としてフォールバック保持すること。これにより引用元の流れが `(抽出できず)` となって文脈が破棄される現象を防止し、直前レスを踏まえた自然な会話展開を可能にする（BAN-169, BAN-171）。
    - `build_img_thread_post_system_prompt` と `build_img_thread_post_prompt` で同一の細かな禁止事項（AI煽り反論、未解析動画、他作品ミーム、文頭オウム返し、自己投影ミラーリング等）が二重・三重に重複していた構成を整理。システムプロンプト側は役割定義・人格口調反映・3段階推論（Step A: 文脈把握、Step B: カウンター方針、Step C: サニタイズ）・JSON出力順序にスリム化し、個別制約はユーザープロンプト側に集約すること。
    - システムプロンプトにおいて、JSON出力形式として必ず最初に `thought` キーを出力して3段階推論を完了させてから `quote_post_numbers` と `comment` を出力するよう順序を明示誘導し、細かな禁止ルールの過密によるペナルティ回避からトートロジー（疑問文の言い換え逆質問）へ退行するのを防止すること。
54. **事前分析への調査判定（research_decision）同梱とLLM呼び出し二重化の防止 (BAN-115, BAN-141, BAN-168)**:
    - 事前分析 `_analyze_pre_reply_context`（`prompt_manager/analyzer.py`）の戻り値 `intent_info` にオプショナルキー `research_decision`（`Optional[dict[str, Any]]`: `should_research: bool`, `mode: str`, `search_query: str`）を同梱し、同一文脈に対する調査判定モデルの二重呼び出しを防止すること。
    - 事前分析プロンプト（`analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt`）およびスキーマ（`_PRE_REPLY_ANALYZER_SCHEMA`）に `research_decision`（`mode` enum: `none`, `simple_search`, `browser_search`, `image_search`, `img_thread`、`search_query` 最大100文字）を定義・指示すること。モデルの過度な拘束を避けるため外側スキーマの `required` には含めずオプショナル維持すること。
    - スキップ時（`skip_intent=True` 等のカジュアル挨拶・相槌）は `{"should_research": False, "mode": "none", "search_query": ""}` をセットし、LLM未出力時や判定不能時は `None` を安全にフォールバックすること。
    - BAN-115 / BAN-141 / BAN-168 多重防御ガード: 困りごと相談や意見募集等の文脈でモデルが `should_research: False` を返しても、既存の調査保護を妨げないこと。
    - **【外部調査の判断基準】（Decision boundary）の維持**: 判定項目8の直後に置いた「外部調査の判断基準」ブロックを削除・弱体化しないこと。①時事・価格・発売日・スケジュール・人物/作品/団体/製品の素性や最新動向・法令/仕様の現行内容・スポーツ結果・ネットの話題は、変化/知識の古さ/未知の可能性が少しでも（目安1割超）あれば調べる側へ倒す、②『最近』『今』『新しい』への言及も同様、③迷ったら調べる、④対話相手・参加者の名前/素性、Bot自身の過去発言・行動（引用された自分の発言への質問を含む。思い出し担当）、文脈内で完結する雑談・ボケは `should_research=false`、⑤『調べて』『検索して』の明示は true。この基準はプロンプト文面のみで実現し、`research_decision` 欠落時の既定（`None` / スキップ時 `should_research=False`）を true に変えてはならない（誤発火の温床）。回帰テスト: `tests/test_analyzer_research_decision.py::TestResearchDecisionCriteriaPrompt`。

55. **ユーザー訂正検出（user_correction）・否定済み主張レジストリ・会話状態メモ描画 (BAN-115, BAN-137)**:
    - 事前分析スキーマ（`_PRE_REPLY_ANALYZER_SCHEMA`）およびプロンプト（`analysis_prompts.py` の `build_pre_reply_analyzer_system_prompt`）に `user_correction`（`Optional[dict[str, Any]]`: `is_correction: bool`, `retracted_claim: str`, `corrected_fact: str`）を定義・追加し、ユーザーがBotの直前発言の誤りを否定・訂正した際（例: 『ちがうよ』『言ってない』等）に確実に検出すること。
    - 否定済み主張レジストリ（`denied_claims.py`）を新設し、`register_denied_claim`, `list_denied_claims`, `clear_denied_claims`, `annotate_context_lines` を配備。検出された訂正主張を即時登録し、TTL（デフォルト6時間）および最大件数（デフォルト10件）で管理し、Bot発言行への否定済み注釈付与（8文字以上対象、重複付与防止）を行うこと。
    - プロンプトビルダー（`builder.py` の `_build_user_prompt`）において、直前の訂正がある場合は【直前の誤りの訂正指示（最重要）】ブロック（短く素直に認めて謝罪、言い訳厳禁、再主張禁止）、過去の否定済み主張が存在する場合は【否定済み主張の再主張禁止リスト】、さらに作業記憶に `conversation_state` が存在する場合は【現在の会話状態メモ】（話題、確定した事実、否定された内容、未解決の問い、相手の様子）を確実にプロンプトへ描画すること。
56. **固有名詞グラウンディング検査と架空人物名捏造の抑止 (BAN-168, BAN-169, BAN-171)**:
    - 生成プロンプト常設ルール（`system_prompts.py` の `build_chat_reply_system_prompt` ルール14および `USER_REPLY_BASE_PARTS`）に「会話に出ていない人物名を比較や例え話のために捏造しないこと」を追加（『〜の二番煎じ』『〜みたいな』『〜選手』等の構文で架空の外国人名・人物名を合成せず、描写のみで表現）。
    - `parser/validation.py` に `find_ungrounded_person_names` を配備。コンテキスト（`allowed_terms`）に存在しない中黒連結カタカナ名（パターンA）および人物文脈共起カタカナ語（パターンB）を決定論的に検出すること（`stopwords`, `persona_names` による保護を含む）。
    - `parser/cleaner.py` に `strip_ungrounded_person_sentences` を配備し、未グラウンディング人名を含む文のみを句点・改行単位で除去してサニタイズすること。
    - `prompt_manager/retry_prompts.py` に `build_anti_name_hallucination_retry_prompt` を配備し、検出された架空人名を明示して描写のみでの再生成を促すリトライプロンプトを提供すること。
57. **全フロント共通行動ログ（bot_activities）の想起検出と台帳ブロック注入**:
    - `prompt_manager/analyzer.py` の `_detect_activity_recall` により、過去の行動への言及・場所マーカー（X、ふたば、リアクション/スタンプ等）と疑問表現を含むユーザー発言を決定論的に検出（`intent_info["activity_recall"] = True`, `intent_info["activity_front"] = "x" | "futaba" | "discord"`）。
    - 行動想起検知時（`ACTIVITY_LOG_ENABLED` 有効時）は事前分析および記憶選別の強制スキップを解除（`skip_intent = False`, `skip_memory = False`）。
    - `prompt_manager/builder.py` の `_build_user_prompt` において、`memory_store.search_bot_activities` を呼び出し（時間表現「さっき」「今日」「昨日」に応じた時間窓設定、キーワードマッチングおよびフロント一致による決定論的スコアリング）、上位件数をプロンプトへ注入：
      ```
      【過去の行動・体験の記録（台帳）】
      - [{日時} / {front}] {summary}（{detail があれば補足}）
      答え方: この記録に基づいて、自分の体験として答えること。記録に無いことは作らないこと。他の人の発言は要旨の範囲でのみ触れ、逐語で引用しないこと。
      ```
    - LLM呼び出しを追加せず、決定論的マーカー判定とスコアリングで高速かつ安全に想起文脈を供給し、他者の発言の逐語引用を抑止して自身の体験として回答させること。
58. **会話履歴の決定化（チャンク連結・厳密安定ソート）・思い出しブロック発火適正化・出来事メモ描画 (BAN-143, BAN-159)**:
    - **会話履歴の決定化（`history_helpers.py`）**: `_merge_consecutive_assistant_chunks` により、同一返信・同一宛先のBot連続分割送信チャンク（時刻差120秒以内）を1つのターンに結合し、`line` 表記を `[HH:MM] Assistant (>>宛先): 全文`（返信先がない場合は `[HH:MM] Assistant: 全文`）に統一。さらにターン列の返却直前に `_finalize_context_turns` で `(created_at_ts, message_id)` による昇順厳密安定ソートを実行し、分割チャンク割り込みや順序混線による話者・行為者取り違えを根絶すること。
    - **思い出しブロック発火適正化（`builder.py`）**: `is_reply_to_bot`（または過去発言言及）を満たし、かつ `intent_info.get("recall_question")` または `_looks_like_recall_question(user_text, is_reply_to_bot=True)` が真である場合のみ発火する AND 条件に厳格化。回想質問ではない通常返信ターンでの誤発火を完全に抑止し、`log.info("[RECALL_BLOCK] fired=%s reason=%s", fired, reason)` を出力すること。言及メモが「記録なし」の場合の指示制約として「自分の発言に書いた気持ちや内容をそのまま言い換えて答え、「誰が何をしたか」等の出来事・行為者の推測や空白を推測で埋める再構成は厳禁です」を注入すること。
    - **出来事メモ描画（`builder.py`）**: `working_context` または `conv_state` 内の `recent_events: list[dict[str, str]]`（最大5件）を取得し、`EVENT_MEMO_ENABLED=True` 時に会話履歴ブロック直前に `【直近の出来事メモ（行為者と行動）】` として描画。誰が何をしたかの客観的な事実関係を明示し、第三者への行為者取り違えを多重防御すること。
59. **構造化JSON切断時の comment 決定論的サルベージ (BAN-105, BAN-160)**:
    - `parser/structured.py` の `call_ollama_json` において、`salvage_comment_on_truncation: bool = False` オプションを提供。
    - トークン上限到達による切断（`truncated=True`, `done_reason="length"`）やパース例外時、オプトインされている場合は `_salvage_comment_from_truncated_json` により、閉じクォートまで完結しているトップレベルの `"comment"`、`"quote_post_numbers"`、`"quote_reply_numbers"` を決定論的正規表現・JSONアンエスケープで救出。
    - サルベージ成功時は `{"comment": str, "quote_post_numbers": list[str], "quote_reply_numbers": list[int], "thought": "", "_salvaged": True}` を即座に返却し、無駄な再生成ループ（BAN-160）や再試行による投稿文劣化（ふたば507型事故・BAN-105）を完全に防止すること。
    - `comment` 自体が未完（閉じクォートなし）の場合やオプトインされていない場合は従来通りリトライまたは例外へ進み、100%の後方互換性を死守すること。
60. **ふたばプロンプトへのペルソナ語尾解除条項の適用境界明示**:
    - `board_prompts.py` の `build_img_thread_post_prompt` および `build_img_thread_followup_post_prompt` において、ペルソナ語尾解除条項（「※ただし大喜利ボケ・なりきり代弁・定型句投下時は切れ味・オチを最優先してペルソナ語尾を外して構いません」）の末尾に、適用範囲を限定する境界明示文（「なお、ペルソナの語尾を外してよいのは、大喜利・なりきり代弁・定型句を投下する場面だけです。共感・感想・ツッコミなどの通常の返信では、話題が真面目な内容でも一人称と語尾を維持してください。」）を配備。
    - 大相撲スレッド等の真面目な共感・感想の返信において、Botがペルソナを脱ぎ捨てて常体・掲示板口調に退行する事故を抑止し、大喜利と通常返信の境界を明確化すること。
61. **ペルソナ口調検査（口調喪失パターン仕様 v2）純粋関数の実装**:
    - `parser/validation.py` に `find_persona_tone_loss` を配備。会話返信においてペルソナ（語尾・一人称）が失われ、匿名掲示板の常体（〜だな、〜だろな等）、方言・カジュアル常体（〜やん、〜じゃん等）、である調・論文口調（〜である、〜と言える等）、無個性な説明口調（「〜です。〜ます。〜でした。」のみの連続）、体言止めの連続羅列、一人称のすり替わり（「私」「俺」等）、およびマーカー付き強常体混在型に退行する事象を決定論的に検出する仕様 v2 へ拡張。
    - **カスタム markers 引数対応**: キーワード引数 `markers: Iterable[str] | None = None` をサポート。呼び出し側から任意の語尾マーカーコレクションを指定可能。`None` または空（空白要素のみ含む）の場合は既定の9語（`_PERSONA_TONE_MARKERS`）へ自動フォールバックし、後方互換性を100%維持。
    - **判定条件 (仕様 v2)**:
      1. 引用行（`>`）、コードブロック（```...```）、インラインコード（`...`）、URLを判定対象から除外。20文字以下は短文免除（`len(target_text) <= exempt_max_chars`）。
      2. **条件A'（一人称すり替わり、パターン6）**: 鉤括弧（「」『』）を除去した本文に `first_person` が無く、禁止一人称（私/俺/僕/自分/あたし/わたし）が含まれる場合は B 判定を待たずに即時 `"first_person_mismatch"` を返却。
      3. **条件A（マーカー確認）＆ 条件B4（混在型、パターン7）**: 実効ペルソナマーカー（指定された `markers` または `_PERSONA_TONE_MARKERS`）または一人称が本文に存在する場合でも、強い常体文末（`だからな`, `だろな`, `やん`, `じゃね`, `だぜ`）が2つ以上あれば `"strong_plain_despite_marker"` を返却。1つ以下なら合格（`None`）。
      4. **マーカー無しの検査（条件B）**:
         - **B1 (`plain_endings_without_persona_marker`)**: 掲示板・カジュアル常体文末（`だろ`, `だよ`, `やん`, `やろ`, `やで`, `じゃん`, `っしょ`, `だべ`, `せやな`, `せや`, `わけない`, `わけだ` を追加拡張した `_PLAIN_TONE_ENDING_RE`）の検出。
         - **B2' (`dearu_tone`)**: である調・論文口調文末（`である`, `であった`, `に過ぎない`, `と言える`, `ということだ`）の検出。
         - **B2 (`bare_explainer_tone`)**: 3文以上すべてが「です/ます/だ＋。」で終わり感情記号（…/！）を欠く説明口調の検出。
         - **B3 (`all_plain_sentences`)**: 2文以上すべてが非丁寧・非ペルソナ形文末で、感情記号（…/！）を欠く全常体・断片羅列の検出。
    - 判定は完全決定論的な文字列処理のみで動作し、LLM推論を消費せず高速に動作する。フロント側の配線（`front/discord/reply/reply_core.py`）への完全な後方互換性を維持。


---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの検証スクリプト
  ./check.sh
  # または
  PYTHONPATH=mock_libs:. pytest harness/agent/prompt_parser/tests/
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/prompt_parser/
  ```
