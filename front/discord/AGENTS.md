# 💬 Front Discord サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`front/discord/`）は、**Discord API 入出力・イベント受信・返信生成フロー・スラッシュコマンド/コンテキストメニュー・ライフサイクル制御**を担当する独立サブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **Bot 起動・エントリポイント（`main.py`, `cog.py`）**:
   - Discord Bot プロセス起動、多重起動ロック、`OllamaChatCog` 構成。
2. **イベント・返信フロー（`events.py`, `research_events.py`, `reply/`）**:
   - `on_message` / リアクション受信、返信要否判定（`reply/classifier.py`）、`DiscordAdapter` と `AgentBridge` を介した `AgentOrchestrator` 呼び出し（`reply/reply_core.py`）、返信後記憶抽出・Prospective Memory 未来予定抽出、定期フォローアップ発言ループ（`_prospective_followup_loop`）。
   - **返信コア (`reply/`) 構造**:
     - `reply_core_common.py`: 共通定数、プロキシ関数、例外定義、ユーティリティ
     - `reply_core_prompts.py`: システムプロンプト、思考適応、行動スタイル、境界注記 (`ReplyPromptMixin`)
     - `reply_core_runtime.py`: MessageRuntime構築、時刻/天気即時応答、履歴管理 (`ReplyRuntimeMixin`)
     - `reply_core_sending.py`: Discord送信作法、相槌絵文字、直接返信判定 (`ReplySendingMixin`)
     - `reply_core_episode.py`: エピソード記憶、長期記憶、Prospective Memory、要約更新 (`ReplyEpisodeMixin`)
     - `reply_core_model_call.py`: モデル呼び出し、再試行・リカバリ、排他・直列化制御 (`ReplyModelCallMixin`)
     - `reply_core_break.py`: 自発発話・ブレイク返信生成 (`ReplyBreakMixin`)
     - `reply_core.py`: 上記6機能Mixinを多重継承した合成クラス `OllamaChatReplyCoreMixin`（本体は司令塔 `_generate_normal_reply` のみ保持）。送信前バリデーション順序（①意味検査 ➜ ②固有名詞グラウンディング ➜ ③ペルソナ口調検査 ➜ ④短文化謝罪）を統括。
3. **コマンド・ゲーム UI・管理機能（`admin.py`, `commands/`, `lifecycle/`）**:
   - 管理コマンド（`/bot`, `/bot_start`, `/bot_stop`, `/bot_status`, `/bot_next_reply`, `/slang`）、画像検索・同定（`/image_search`）、言葉からの画像検索・採点（`/image_find`）、歌唱（`sing.py`）、20の扉（`twenty_doors.py`）、ウミガメのスープ（`umigame.py`）、モデル呼び出し・事前調査ライフサイクル。
4. **Discord 共通ヘルパー（`common/`, `utils.py`, `adapter.py`）**:
   - メッセージ属性抽出、コンテキストキャッシュ、Bot 呼称判定、添付メディア抽出、`DiscordAdapter`。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `front/discord/` 配下のファイルのみを編集すること。**
   - 他プラットフォーム（`front/hutaba/`, `front/x/`）や `front/` 直下、`harness/agent/*`, `ollama_bot/` のファイルを直接編集してはならない。
2. **【他プラットフォーム直接 import の禁止】**:
   - `front.hutaba` や `front.x` を直接 import しないこと（連携が必要な場合は `front` ブリッジ層または互換インターフェースを経由すること）。
3. **【`front/` を `PYTHONPATH` に入れることの厳禁】**:
   - `front/discord` が `discord.py`（`import discord`）と同名になるため、`PYTHONPATH` には必ずリポジトリルート（`.` と `mock_libs`）のみを指定すること。
4. **【公開・デプロイスクリプトとの同期義務（項目166）】**:
   - ディレクトリ構造変更や新規共通モジュール追加時は、`customize_for_github.py` の `allowed_items`、`mypy.ini`、`pytest.ini`、`discord-ai-bot.code-workspace` の同期漏れがないか確認すること。
5. **【プロンプトトークン数削減の設計基準（BAN-137）】**:
   - システムプロンプト（`_reply_system_prompt`）にキャラクター設定・口調ガード（`OLLAMA_REPLY_STYLE_GUARD`）・追加ルール（`OLLAMA_REPLY_EXTRA_RULE`）が集約されているため、ユーザープロンプト末尾（`【最終口調ガード】` / `【最終追加ルール】`）や話題転換（BREAK）プロンプト内で同一のルール指示を二重・三重に重複注入してはならない。
   - `_clean_duplicate_prompt_instructions` により、モデル送信直前にシステムプロンプトと重複するガード記述を確実に除去し、不要なトークン消費（1万トークン超）と長大コンテキストによる推論遅延（70秒超）、Discordメッセージ参照無効化（`code=50035`）を防止すること。
6. **【返信後バックグラウンドタスクの実行順序規約と直列化制御（BAN-143）】**:
   - 返信送信完了後の重いLLMタスク（長期記憶抽出 `extract_and_store_memory`、関係性一言所感生成 `_update_user_thought_async`、チャンネル要約更新）は、ローカルLLMへの同時リクエスト競合による深刻な遅延を防ぐため、必ず直列化して実行すること。
   - **初期遅延（Initial Delay）**: 返信直後は `MEMORY_EXTRACTION_INITIAL_DELAY_SEC`（デフォルト3.0秒）の待機を挟み、軽量な感情更新等の完了を優先させる。
   - **ユーザー返信最優先譲歩（Yield）とビジーループ・Heartbeat遮断防止**: バックグラウンドタスク実行前には必ず `_yield_to_user_replies` を呼び出し、後続のユーザー返信推論中（`_active_user_reply_count > 0`）のみならず、前処理（Web調査・文脈判定・事前分析）中（`_active_user_reply_turn_count > 0`）や返信キュー待機中（`_reply_queue.qsize() > 0`）を含むビジー状態（`_is_user_reply_busy()`）の間は即座に待機・譲歩する。タイムアウトを迎過しても強行実行せず、安全に待機を延長するか、返信完了イベント（`_user_reply_done_event`）の通知を待機してメインLLM/Visionモデル推論中の並行リクエスト競合を完全に遮断すること。その際、`_user_reply_done_event` がセット済みのままカウントが残る競合状態で `while` ループがノーディレイのビジーループに陥りイベントループを占有（Discord Gateway Heartbeat 遮断）しないよう、`event is not None and not event.is_set()` の場合のみ `event.wait()` で待機し、set 済みまたは None の場合は必ず `await asyncio.sleep(0.2)` を経由して他タスクへ確実に制御を譲ること。また、`_process_queued_message` の開始・終了、キュー投入時、および各推論呼び出しにおいて `_refresh_user_reply_done_event()` により `_user_reply_done_event` のライフサイクルを統一すること。
   - **相互排他・直列化制御**: `_ensure_background_llm_serialization` により、所感生成実行中は記憶抽出タスクを待機させ、記憶抽出実行中は所感生成タスクを待機させ、ローカルLLMの同時呼び出しを抑止すること。
7. **【事前分析直前のVRAM早期アンロード防止とモデル解放ライフサイクル（BAN-137）】**:
   - `_check_context_flow` の直後でユーティリティモデルを一律解放する際、直後の事前分析（Pre-reply Analyzer）で使用される `OLLAMA_MIDDLE_MODEL` は解放対象から除外すること。
   - 早期解放によって直後に同一の Middle-LLM が再ロードされ、無駄な待機時間（約12秒）が発生するVRAMスラッシングを防止する。
   - `OLLAMA_MIDDLE_MODEL` の解放は、事前分析が完了した直後（`OLLAMA_RELEASE_MIDDLE_MODEL_AFTER_ANALYSIS`）またはターン全体の終了時（`finally`）に確実に実施すること。
8. **【User-First Resolution における調査対象すり替え防止と全名詞走査要件（BAN-21, BAN-61, BAN-102, BAN-131）】**:
   - サーバーメンバー照合（User-First Resolution）によりWeb検索・外部調査をキャンセルする際、単一単語（`candidate_term`）のみの照合ではすり抜けが発生するため、`target_subject`、`candidate_term`、`named_terms` 全体および発言本文から抽出された固有名詞候補をすべて走査して Guild メンバー名（ニックネーム・ユーザー名）および DB プロファイルと照合すること。
   - いずれかの候補単語が実在メンバーと一致した場合、外部Web調査を即座にブロックし、`[USER_FIRST_RESOLUTION] BLOCKED: term='<term>' is guild member` とログ出力してサーバーメンバーを最優先すること。
   - ただし、調査対象がメンバーに一致しなかった場合、メッセージ全文から無関係な単語をメンバー解決して調査対象をすり替えたり、Web検索を不当にキャンセルしてはならない（例: 「10万ボルト 50mAをピカチュウから食らったら人間はどうなるの？」で「人間」をメンバー解決してボルトのWeb検索をキャンセルするなど）。
9. **【Pre-reply Analyzer LLM判定尊重とルール誤爆調停規則（BAN-115）】**:
   - Pre-reply Analyzer LLM が明確に `external_research=False`（調査不要）と判定した場合は、ルール側の単語一致フラグがあっても外部Web検索を実行せず、`reply_research_planner` を介して安全に通常の返答生成フローへフォールバックすること。
   - LLMの「調査不要」判断によるWeb検索の抑止理由（`[RESEARCH_PLANNER] Pre-reply Analyzer LLM decided external_research=False; suppressing rule-based research trigger: ...`）をログ出力すること。
10. **【外部調査返信におけるサマリー丸写し・一方的講釈の禁止と対話校正リトライ（BAN-168）】**:
   - 外部調査を伴う返信において、LLMが調査サマリー（`research_summary`）の文章を引き写して一方的な解説レポートを出力し、ユーザー発言へのリアクション（体験や感想への共感・受容）が欠如したまま送信されることを禁止する。
   - `_looks_like_raw_research_dump` により、サマリーからの20文字以上の連続一致または複数文一致を検出し、ユーザー発言が質問ではない日常報告・雑談である場合にリアクション語を欠く一方的講釈を確実に検知する。
   - 検知時は `_build_research_conversation_retry_prompt` により相手の発言へのリアクションや共感から始める自然な会話文への校正を命じるリトライを行い、過剰な講釈を省いた1〜3文の会話調返答を出力すること。
11. **【ふたば自発巡回タスク起動管理とユーザー返信優先度連携（BAN-143関連）】**:
   - `front/hutaba` に追加される自発スレッド巡回・投稿ループ（`_img2chan_proactive_post_loop`）は、`events.py` の `_ensure_background_tasks_started` において `HUTABA_PROACTIVE_POST_ENABLED` フラグに基づき安全に起動登録（`_start_background_task_if_needed`）すること。
   - 対象メソッドが未定義または無効な場合は安全に no-op とし、例外や不要なタスク生成を防止すること。
   - ふたば側の処理が Discord ユーザー返信を最優先できるよう、`_is_user_reply_busy()`（`_active_user_reply_count`, `_active_user_reply_turn_count`, `_reply_queue` 監視）や `_yield_to_user_replies`（および `_user_reply_done_event`）との連携により、ユーザー返信のキュー待機・前処理（Web調査・事前分析）・生成中は確実に待機・譲歩する直列化・優先度制御を破綻なく動作させること。
12. **【事前分析（`topic_origin`）による誤BREAK補正・内省メモ注入と並行スレッド誤警告防止（BAN-90, BAN-136）】**:
   - `_check_context_flow` がタイムアウト等で誤って `BREAK` と判定した場合でも、事前分析（`_analyze_pre_reply_context`）で `topic_origin`（話題の起点・背景）が特定された際は、`_format_reply_deliberation_note` の `[返答前の内省メモ]` に `topic_origin` を確実に注入すること。
   - `_generate_break_reply_from_memory` において、`topic_origin` に含まれるユーザー名やトピックと重複するスレッドを `active_threads_data` から除外し、直前の会話の続きやツッコミである発言を「別スレッド（混同禁止）」として不当に警告・文脈切断しないこと。
   - `build_break_prompt_parts` に `topic_origin` を渡し、プロンプト内の「別話題である可能性があります」を「直前の会話を踏まえた発言・ツッコミ」へと安全に補正すること。
13. **【返信見送り時（`ignored=True`）の受動的長期記憶抽出および要約更新（BAN-143関連）】**:
   - クールダウンや確率制御により返信を見送った通常メッセージ（`ignored=True`）であっても、発言に明確な予定・食事・行動宣言（「〜食べようかな」「今日は〜」等）が含まれる場合、または一定間隔（`MEMORY_PASSIVE_SUMMARY_INTERVAL = 15`）に達した際は、`_maybe_schedule_passive_memory_update` によりバックグラウンドで受動的長期記憶抽出（`maybe_extract_passive_channel_memory`）およびチャンネル要約更新（`maybe_update_channel_summary`）を実行すること。
   - バックグラウンドタスクは `_yield_to_user_replies` と `_bg_llm_lock` によりユーザー返信を最優先に譲歩し、同時LLM呼び出し競合を完全に遮断すること。
14. **【困りごと・意見募集のWeb調査連携と自己投影逃げ・長文丸写しリトライ（BAN-141, BAN-168）】**:
   - ユーザーが困りごとや意見募集、解決策の模索をしている場合（`consultation_flag` / `consultation_problem_solving`）、Web調査による解決ヒント取得が必要なため、Pre-reply Analyzer LLM による調査抑制（`should_suppress_research_by_llm_intent`）から保護し、`final_mode` を `browser_search` にルーティングすること。
   - 相談・困りごと返信（`consultation_flag`）において、「私も〜で困ったことあるんですよ」「僕も〜」といった表面的な共感のみで具体的な解決策が欠落している自己投影逃げ（`_CONSULTATION_SELF_PROJECTION_RE` および動的Config `BOT_FIRST_PERSON` / `OLLAMA_BOT_DISPLAY_NAME` かつ解決マーカーなし）、または240文字超の長文解説・検索サマリー25文字以上の丸写しを `_looks_like_consultation_evasion_or_lecture` で検知すること（特定キャラ名への固定依存を排除し、標準一人称および動的設定に対応）。
   - 検知時は `_build_consultation_solution_retry_prompt` により、調査結果から具体的な解決策・代替案・ヒントを1〜2点だけ選び、1〜3文（60〜160文字程度）の簡潔な会話調でアドバイスする再生成リトライを実行すること。
15. **【複数人雑談における非連続メッセージ分離とRecency Bias防止（BAN-168, BAN-177）】**:
   - 複数人が同時に会話しているチャンネルにおいて、直前の無関係な別ユーザーの発言語彙を引きずるRecency Bias（直前語彙の誤結合）および「置いてけぼり」等のメタ逃げ応答を防止すること。
   - `format_discord_context_line_with_relation` および `format_context_lines_with_relation_labels` により、会話履歴の各行に返信先参照（`reference`）に応じた構造化ラベル（`[>>ユーザー名 への返信]` / `[独立発言]`）を付与し、モデルが返信親子関係と独立した発言を明示的に識別できるようにすること。
   - `build_different_author_boundary_note` / `_apply_different_author_boundary_note` により、返信対象メッセージ直前の発言者が異なり、かつ引用・返信関係がない場合、プロンプトに境界ガード（`※直前にある別ユーザーの発言（〇〇の発言）は別件の話題です。返信対象である {target_author} の発言内容にのみ集中し、直前発言の語彙を混入させないでください。`）を確実に注入すること。さらに、直前メッセージより前の会話履歴（`prior_context_items`）が存在する場合、「〇〇の話題が△△に繋がる」等と直前の別話題と対象メッセージを無理やり関連付けたり合体（キメラ合成）させることを厳禁とし、直前発言より前の会話文脈および対象者の発言の続きとして反応させるガード指示を付加すること。
   - **System内心メモの別ユーザー誤認防止・リプライ親探索除外・ステールメモ残留防止**:
     - `channel_context_cache` 内に保存される `role="system"`（`System: (内心のメモ: ...)`）は人間の発言ではないため、`build_different_author_boundary_note`（別ユーザー境界注記判定）および `_apply_different_author_boundary_note` の履歴走査において別ユーザーとして誤判定してはならず、直前の実際の発話（人間ユーザーまたはBot）を参照すること。
     - `find_reply_parent_from_cache`（リプライ親探索）においても `role="system"` や `(内心のメモ` は探索対象から確実に除外し、また存在しない `target_message_id` が指定された際にキャッシュ末尾の無関係な発言やSystemメモを親として誤返却しないよう安全に `None` を返すこと。
     - `recent_context_items` では、`min_keep` による古い履歴救済時に期限切れの `role="system"` 内心メモを保持対象から除外し、有効期間内のSystemメモについても最新の1件のみに限定することで、長大な内心メモの多重残留によるプロンプト長圧迫を防止すること。
   - `looks_like_incomplete_topic_opener` および `collect_same_author_burst_texts` により、『〇〇さあ』『〇〇ってさ』『〇〇なんだけど』等の未完結な話題切り出し短文で返信トリガーが発火した際、直後に同一話者が連投したキャッシュ内の後続メッセージ（2行目・3行目等）を自動待機・回収（`original_user_text` / `effective_user_text` へ改行結合）し、話題切り出し短文単体への誤った先走り返信や未来メッセージとしての切り捨てを防止すること。
   - `looks_like_meta_ignorance_reply` および `sanitize_meta_ignorance_reply` により、「置いてけぼりになっちゃいます」「急に〇〇と言われても分からない」等のメタな無知・疎外感表明を検知し、等身大の共感・相槌へと安全に書き換えること。
16. **【メインモデル実行直前の middle-llm 自動アンロードと VRAM 最適化規約（BAN-170）】**:
   - `_call_reply_model_or_fallback` の開始直後（推論リトライ試行ループに入る前）に、`OLLAMA_MIDDLE_MODEL` で設定されたユーティリティモデル（`middle-llm` 等）を `release_ollama_model` で確実にアンロードすること。
   - 直前の事前分析（Pre-reply Analyzer）やWeb調査判定で GPU にロードされた `middle-llm` を解放してからメインモデル（27B等）を呼び出すことで、VRAM 競合によるメインモデルの CPU オフロード（70%等）低速固定化を防止し、メインモデルの利用可能 VRAM を最大化（最大GPUオフロード）すること。
   - `research_events.py` におけるモデル呼び出しタイムアウト（`ReplyModelTimeoutError`）発生時は、`except` ブロック内で `_release_middle_model_if_needed()` を実行してから `_send_timeout_fallback_reply` を送信して安全に return すること。タイムアウト処理構文が正常系フローへ漏出・多重解放するインデント事故を絶対に起こさないこと。
17. **【スラング自動調査・DB登録コマンド `/slang` の仕様と運用規約（項目47, 50, 560）】**:
   - スラッシュコマンド `/slang`（メソッド名: `slash_slang`）は、ネット掲示板・コミュニティ特有のスラングをWeb検索（`research_dispatch`）および LLM（`call_ollama_json` / `middle-llm`）で自動調査・構造化解析し、`memory.db`（`load_slangs_from_db` / `upsert_slang`）へ永続化する。
   - **アクション仕様**:
     - `learn`: 単語のWeb調査とLLM解析を行い、DBへ登録・更新（管理者専用 `is_bot_admin_user`）。処理開始および完了ログ（`[slang_admin] Starting slang research ...` / `[slang_admin] Successfully learned slang ...`）と登録結果Embedを出力。
     - `show`: 登録済みスラングの詳細情報（カテゴリ、検索クエリ除外、固有名詞判定、意味、Bot向けガイダンス注記）をEmbedで表示。未登録時は登録方法を案内。
     - `list`: 登録済みスラングの総件数および単語一覧プレビュー（25件超は省略表示）をEmbedで表示。
   - **検索ノイズ・固有名詞誤読防止（項目47, 50）**:
     - 質問の前置き略語（`prefix_slang`: すみフラ、うろフラ等）やリアクション定型句（`reaction_meme`: なそ等）は `strip_from_query=True` として検索語から除外しノイズを防止する。
     - 単なる俗語・前置きで作品名やキャラ名等の固有名詞でない場合は `is_proper_noun=False` とし、Bot向けガイダンス（`guidance`）によりキャラクター名と誤認させない注意書きをプロンプト注記へ反映する。
   - **メソッド命名規約（項目560）**:
     - `@app_commands.command(name="slang")` に対し、メソッド名は `slash_slang` を厳格に適用すること。
18. **【2段階Visionキャプション・パイプラインおよびGPU VRAM即時解放規約（BAN-15, BAN-170, BAN-180）】**:
   - Discord に画像が添付された際、返信生成モデル全体をVisionモデルに差し替えてしまうとメインモデルのペルソナ・口調が失われ、GPU VRAMにVisionモデルが残留してメインモデルと競合（CPUオフロード低速化）する。
   - `build_message_media_context` 内の前処理として `describe_images_with_vision_model`（`qwen3-vl:8b`等）を呼び出し、客観的キャプション・文字起こしのみを抽出した上で、処理完了時（成否・例外問わず `finally`）に直ちに `release_ollama_model(vision_model, timeout_sec=5.0)` でアンロードすること。
   - Visionキャプション時のタイムアウト初期値はCold Start（モデル初回ロード＋画像エンコード＋推論）に耐えられるよう `60.0` 秒（`OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC`）とし、15秒等の短時間でのタイムアウトによる「中身が見えない」誤返信を防止すること。
   - KVキャッシュの確保負荷とVRAM消費を最適化するため、コンテキストサイズは `4096`（`OLLAMA_VISION_NUM_CTX`）を指定して `call_ollama` に渡すこと。
   - キャプション抽出成功時は `MediaBundle.images` を空リストとしてメインモデル（テキストLLM）に渡し、`analysis_text`（`添付画像{idx}の解析内容:\n{desc}`）経由で画像内容を伝搬させることで、メインモデル本来のペルソナと口調で返信を生成すること。
   - `_call_model` においても `VISION_PIPELINE_MODE == "caption_then_main"` かつメインモデルが非Visionの場合は画像を切り離してメインモデルを呼び出し、モデル差し替えによるペルソナ喪失とVRAM競合を多重防御すること。
   - 音声・動画も画像と同様に前処理キャプション方式（`message_media_av.py`）に従い、メインモデルの介入前に客観的テキスト化して `analysis_text`（`添付音声{idx}の文字起こし:\n{text}` / `添付動画{idx}の解析内容:\n{caption}`）に注入すること。メディア処理の失敗やサイズ超過時にもメイン返信ループを中断させてはならず、例外を安全に捕捉してログ出力に留めること。
19. **【画像検索コマンド `/image_search` およびコンテキストメニュー「画像検索」の仕様と運用規約（BAN-161, BAN-162）】**:
   - スラッシュコマンド `/image_search`（メソッド名: `slash_image_search`）およびメッセージ右クリック・アプリメニュー「画像検索」（メソッド名: `context_image_search`）は、指定された画像添付、画像URL、またはチャンネル履歴（過去10件）/対象メッセージから画像URLを検出し、画像検索（Google Lens等）を実行して結果をEmbedで送信する。
   - **タイムアウト防止とメソッド名規約（BAN-161, BAN-162）**:
     - 検索実行前に必ず `await interaction.response.defer(thinking=True)` を呼び出し、Discord API の3秒制限タイムアウトを防止すること。
     - コマンドメソッド名は規約に基づき `slash_image_search` および `context_image_search` を厳格に適用すること。
   - **画像コンテキスト自動補完（Orchestrator連携）**:
     - 通常の会話返信時、`DiscordAdapter.build_reply_task` および `reply_core.py` において、受信メッセージおよび返信先（`latest_ref`）の画像URLを検出し `extra_context["image_url"]` に保持・伝搬させることで、エージェント推論時に自動補完連携を可能とすること。
   - **コンテキストメニュー登録規約（BAN-161, TypeError防止）**:
     - Discord.py ではクラスメソッドに `@app_commands.context_menu` デコレータを直接付与すると `TypeError: context menus cannot be defined inside a class` が発生し起動時クラッシュするため、デコレータは付与せず通常の非同期メソッド（`context_image_search`）として定義すること。
     - コンテキストメニューとしての登録は `OllamaChatCog.__init__` 内で `app_commands.ContextMenu(name="画像検索", callback=self.context_image_search)` としてインスタンス化（`self._image_search_menu`）し、`OllamaChatLifecycleBaseMixin._context_menus()` に含めてライフサイクル管理（登録・アンロード）を行うこと。
   - **構造化ロギングと観測性（BAN-161, BAN-162, 項目114）**:
     - コマンド受付、対象画像URL解決、実行開始・成否・フォールバック、Embed送信に至る全フェーズで構造化ログ（`[IMAGE_SEARCH_CMD]`, `[IMAGE_SEARCH_MENU]`, `[IMAGE_SEARCH_EXEC]`, `[IMAGE_SEARCH_EVENT]`）を出力し、コンソールおよび `bot.log` での調査追跡性を担保すること。
   - **動的Embedフッター表示（特定キャラ名直書き防止）**:
     - 検索結果Embedのフッターテキストは、`OLLAMA_BOT_DISPLAY_NAME` または `BOT_FIRST_PERSON` を反映し（例: `{名前}画像調査システム`）、未設定時はデフォルトの `"画像調査システム"` を設定すること。特定キャラ名のハードコードを禁止し、マルチペルソナ環境に対応すること。

20. **【返信先（リプライ）画像の自動解決および User-First Resolution 画像検索保護規約（BAN-21, BAN-161関連）】**:
    - 画像レスに対して「これ誰？」「元ネタなに？」と返信（リプライ）された際、受信メッセージ単体ではなく返信先（`message.reference`）のメッセージから画像を自動解決すること（`resolve_message_with_media`）。
    - `reference.resolved` がキャッシュされている場合はそれを優先し、未キャッシュの場合は `channel.fetch_message(ref.message_id)` により非同期フェッチして画像を取得する。
    - サーバーメンバー照合（User-First Resolution）において、返信先ユーザー名（例: 『雑菌』さん）等に反応して画像検索リクエストが誤爆ブロック（`BLOCKED`）されることを防止するため、`is_image_search`（`rule_info["mode"] == "image_search"` または `"image_search_request" in rule_info["rule_reasons"]`）の場合はメンバー名一致による外部調査キャンセルをバイパスし、画像調査を継続すること。
    - `_resolve_research_with_deepdive` において、`mode == "image_search"` の場合は `resolve_message_with_media` で解決した返信先の画像URLを `research_input` に設定して `research_dispatch` に引き渡すこと。また `_normalize_research_mode` の許容リストに `"image_search"` を含め、誤って `simple_search` にフォールバックさせないこと。

21. **【Web調査失敗時における会話型質問へのLLMフォールバック制御および調査コンテキストサニタイズ（BAN-136, BAN-158, BAN-175）】**:
    - Web調査（URL読み取りや検索等）が HTTP 404 やタイムアウト等で失敗（`_is_research_result_insufficient`）した場合でも、直前会話やコンテキスト内に情報が存在し、かつユーザーが会話型の質問（「どんな内容だった？」「どういうこと？」等）をしている場合は、機械的な固定文（「URL先まで確認したけど、十分な情報は取れなかったんですよ…！ 理由はHTTP 404。」等）で即終了させず、手元の会話コンテキストを踏まえた通常LLM推論（キャラクターらしい自然な返答）へ安全にフォールバックすること。
    - ただし、URL単体貼り付けや明示的なURL確認命令（「URL」「リンク」「このサイト」「そのサイト」「記事見て」「ページ見て」）の場合は、引き続き `_build_research_failure_reply` による失敗定型文を返信すること。
    - `_build_research_context_text` で調査用コンテキストテキストを構築する際、System内部メモ行（`System: (内心のメモ: ...)` や `System:`、`system:`）を除外して余計なURLの漏洩・誤検知を遮断すること。

22. **【may.2chan.net リサーチ判定連携および複数URL指定時の先頭1件限定規約（項目172, 179, 184）】**:
    - ふたば掲示板スレッド（`img.2chan.net` および `may.2chan.net`）のURLが入力された際、調査判定で `browser_read_url` への誤流出によるレイアウト崩れを防ぎ、確実に `img_thread` モードとして解決すること。
    - メッセージ内に複数のスレッドURLや汎用URLが混在して入力された場合、意図しない後続URLの採用や混乱を防止するため、スレッドURLリストの先頭1件のみを確実に `query` / `target_url` に正規化・固定して採用すること。
    - 他モジュールの実装完了を待たずに単体完結できるよう、`_get_img2chan_helper` 経由のドメインヘルパー連携および正規表現（`r"https?://(?:img|may)\.2chan\.net/b/res/\d+\.htm"`）による多重フォールバックガードを担保すること。

23. **【ファクトチェック・外部調査における比喩・ミーム解釈ガード、クエリ表記維持・スラング客観化、およびノイズ引用・人物誤認防止（項目75, 118, 148, 149, 150）】**:
    - **比喩・ネットミーム・スラングの字義通り解釈の厳禁**: 対象文章が比喩表現やネットスラング、誇張表現、ジョークである場合（例: 「天にも昇る心地」等）、言葉通りの物理的・客観的事実として真偽判定を行ってはならない。文脈分析（`_analyze_fact_check_context`）において比喩・ミーム性を検出し、等身大のキャラクター解釈で受け止めること。
    - **固有名詞表記維持と口語スラング客観化**: 検索クエリ抽出において、作品名・キャラ名・固有名詞の表記を勝手に一般的な単語へ書き換えてはならない。また、口語の感情語（「ヤバい」「クソ」等）は客観的検索語へ正規化（`_normalize_search_query_slang`）すること。
    - **ノイズ記事タイトルの引用抑止と人物誤認防止**: 検索結果のスニペットに含まれる無関係な広告・ノイズ記事タイトルを無批判に回答本文に列挙したり、無機質な名詞・作品名を実在人物（「〇〇さん」）と誤認して回答してはならない。

24. **【ContextMenu コールバックの引数シグネチャ制約（TypeError防止）および送信後エラー誤爆防止（response_sentガード）（項目151, 153）】**:
    - `app_commands.ContextMenu` のコールバックは、必ず `(self, interaction: discord.Interaction, message: discord.Message)` の2引数シグネチャ（self除く）を厳守すること（追加引数を定義すると Discord.py のディスパッチで `TypeError` が発生する）。
    - 特殊アクションやコマンドにおいて、ユーザーへのメッセージ送信が完了（`response_sent = True`）した後に、副次処理（メモリ保存・感情更新・ログ出力等）で例外が発生した場合に、外側の `except` で「エラーが発生しました」とエラーメッセージを二重送信・誤爆してはならない。送信完了フラグを管理し、副次例外はログ出力のみで安全に分離すること。

25. **【歌唱機能（Singing Workflow）における Chrome MCP 統合検索・クエリ優先順位と多重防御歌詞抽出（項目142, 168）】**:
    - `sing.py` における歌詞検索では、Chrome MCP ブラウザ検索（優先）と HTTP 直接検索（フォールバック）を自動切り替えること。
    - 検索クエリは「完全一致曲名 歌詞」「アーティスト名 曲名 歌詞」「読点・記号置換」など多角的に生成（`_generate_singing_search_queries`）し、検索ノイズ（目次アンカー等）を排除した多重防御抽出を行うこと。

26. **【ステール（陳腐化）メッセージ送信抑止ガードと60秒以上遅延返信の直接返信強制（項目73, 116）】**:
    - 外部調査（Chrome MCP）やLLM推論遅延に伴い、返信が生成されるまでにチャンネル上で会話が進行した場合のステールガードを徹底すること。自発返信で120秒超かつ新規人間メッセージ2件以上の場合はメッセージを破棄して話題の割り込み・遅延投稿を防止すること。
    - 60秒以上経過した遅延返信においては、エアリプ（誰宛てかわからない通常発言）を禁止し、元の発言への明示的な In-reply-to（直接返信 `message.reply`）を強制して対話の流れを保護すること。

27. **【`[HH:MM]` タイムスタンプ話者分割および検索クエリサニタイズ（BAN-163, BAN-174）】**:
    - 会話履歴（`context_lines`）から話者名を抽出する際、`[22:00] 話者名: 本文` のような先頭タイムスタンプ `^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*` を除去してからコロン分割し、話者名が `[22` などに誤分割される不具合を防止すること。
    - Web調査判定（`reply_research_decider`）でモデルが返した `search_query` から、ユーザー発言本文に含まれていないロール名（`Assistant`, `User`, `System`, `Bot` 等）や対話相手の名前を自動除去し、不要な語彙が混入した検索が実行されることを防止すること。

28. **【削除済み返信対象メッセージの返信破棄規約（NotFound / 50035 Unknown message 対策）】**:
    - ユーザーが投稿直後にメッセージを削除した場合、`_reply_to_message` における直接返信呼び出し時に `discord.NotFound` または `discord.HTTPException`（`code=50035` かつ `Unknown message`）が発生する。
    - メンション（`is_direct=True`）以外の通常の自発・会話返信（`is_direct=False` / `drop_if_target_deleted=True`）においては、チャンネル通常投稿へ切り替えずに `ReplyTargetDeletedError` を送出して返信を破棄（`_send_reply` は `return None`）し、取り消された発言への不適切な言及や会話破綻を防止すること。
    - メンション返信（`is_direct=True`）時のみ、既存どおりチャンネル投稿へフォールバックしてユーザーへの応答性を維持すること。

29. **【Prospective Memory（未来予定・自発発話・リマインド）登録および定期発火背景ループの仕様と運用規約（BAN-143, BAN-154）】**:
    - **会話からの未来イベント・自発意図の自動抽出・登録（`_maybe_extract_and_store_prospective_memory`）**:
      - 返信完了後の背景タスク枠内において、中間モデル（`call_ollama_json` / `OLLAMA_MIDDLE_MODEL`）を用いてユーザー自身の予定・未来イベント・約束・リマインド希望を構造化抽出（`has_future_event`, `event_text`, `when_text`, `due_date`, `cue_keywords`, `action_type`, `intent`, `trigger_timing`, `kind`, `confidence`）する。
      - 登録条件: `has_future_event is True` かつ `confidence >= 0.7` かつ `due_date` が未来日付（本日以降）。
      - 期日設定（動的算出）:
        - `same_day_morning`（当日朝の応援・気遣い）: イベント当日 08:30 JST（期限: 当日18:00）。
        - `same_day_evening`（当日夜の労い）: イベント当日 20:00 JST（期限: 翌日正午）。
        - `next_day_morning`（事後確認・感想尋ね・デフォルト）: イベント翌日 09:00 JST（期限: 3日後）。
      - 意図（`intent`）と種別（`kind`）の汎用化: LLMが抽出した具体的な発話意図を優先採用し、未指定時は `action_type` に応じた自然な文面を生成。種別も `time_based` / `event_based` に対応。
      - 重複・上限ガード（BAN-154）: 同一ユーザーの pending レコード（`list_pending_prospective_memories`）を取得し、`cue_keywords` の重複、または件数が `cfg_int("FOLLOWUP_MAX_PER_USER", 5)` 以上の場合は登録を見送り `log.info` で理由を記録（サイレントドロップ厳禁）。
      - 直列化と譲歩制御（BAN-143）: 実行前には `_yield_to_user_replies` を呼び出し、`_is_memory_extraction_active = True` で所感生成等の同時LLM呼び出し競合を遮断すること。
    - **定期発火背景ループ（`_prospective_followup_loop`）**:
      - `_ensure_background_tasks_started` において `_prospective_followup_task`（周期15分、`PROSPECTIVE_FOLLOWUP_INTERVAL_SEC`）として登録・監視する。
      - 発火時、`_is_user_reply_busy()` を確認し、ビジー状態の場合は次回へ譲歩する。
      - `list_due_prospective_memories` により期日到来レコードを取得し、チャンネルごとに1件、人格システムプロンプト（`_reply_system_prompt()`）を用いて自発発話（事後確認のみならず、応援・労い・リマインド等を含む目的・意図に即した自然な声掛け）を生成する（intentそのものを復唱させない指示を厳格に付与）。
      - 送信成功後に `mark_prospective_triggered(record_id)` を実行。送信失敗時は最大3回まで再試行すること。

30. **【静かな時間帯（生活リズム）における返信抑制・遅延仕様（BAN-139）】**:
    - **通常発言（非メンション）の抑制**:
      - `BOT_QUIET_HOURS_ENABLED` かつ `is_in_quiet_window(now, start, end)` が真の場合、深夜早朝の通常発言に対して返信キュー投入を見送り、返信タスクの起動を遮断する。
      - サイレントドロップを防止するため、見送り時は必ず `log.info("message skipped: reason=quiet_hours (non-mention ignored during sleep window)")` を出力し、`_record_unreplied_message` に記録すること。
    - **メンション・直接リプライの人間らしい応答調整**:
      - 静かな時間帯であってもメンションや強制返信（`force_response=True`）は応答を維持するが、以下の生活リズム保護調整を適用する：
        1. **トークン数（`num_predict`）の半減**: `_reply_num_predict` において通常の半分（`max(64, base_val // 2)`）に抑制し、深夜帯らしい簡潔な返信とする。
        2. **人間らしい応答遅延（sleep）**: メッセージ生成・送信前に `await asyncio.sleep(random.uniform(2.0, 5.0))`（`BOT_QUIET_HOURS_MIN_DELAY_SEC` / `BOT_QUIET_HOURS_MAX_DELAY_SEC`）の待機を挟み、即座の返信による不自然さを排除する。

31. **【返信送信作法（タイピング表示・文単位分割送信・相槌リアクション化）のガード順序と仕様（BAN-168）】**:
    - **送信前ガードの順序厳守（BAN-168）**:
      - 陳腐化判定（stale detection）、60秒超過時の直接リプライ強制、削除済みメッセージ破棄（`ReplyTargetDeletedError`）等のすべての送信前ガードを通過した後に、送信本体の拡張処理を実行すること。
    - **純粋関数 `_split_reply_text` による文単位分割**:
      - 返信文を文末記号（`。`, `！`, `？`, `\n`）を基準に最大 `REPLY_SPLIT_MAX_CHUNKS`（デフォルト3）個に分割する。
      - コードブロック（```` ``` ````）内および URL は途中で絶対に分割せず保護する。
      - 分割数が上限を超える場合は、末尾チャンクに余りを結合する。
    - **タイピング表示付き分割送信（`REPLY_SPLIT_ENABLED`）**:
      - 先頭チャンク: 既存ロジック（直接リプライ要否に従う）で送信。
      - 2つ目以降のチャンク: `async with channel.typing():` の中で `len(chunk) * REPLY_TYPING_SEC_PER_CHAR` 秒（0.8〜3.0秒にクランプ）待機後に通常送信。
      - 送信例外発生時は、残りの未送信チャンクを1つにまとめて即時送信する安全なフォールバックを設けること。
    - **相槌のリアクション化（`REPLY_REACTION_ONLY_RATE`）**:
      - 返信文が12文字以内の相槌パターン（「うん」「そうだね」「なるほど」等）に合致し、かつ `random.random() < REPLY_REACTION_ONLY_RATE`（デフォルト0.0）の場合、文章送信を行わず `_safe_add_reaction` で感情に応じた適切な絵文字（👍, 👀, 😊, 😮, 😢 等）を付与して終了する。
      - メンションや強制返信（`is_direct=True`）時はリアクション化から除外し、確実にテキストメッセージとして返信すること。

32. **【調査判定の統合利用と感情・所感採点の間引き仕様（BAN-115, BAN-141, BAN-143, BAN-168）】**:
    - **調査判定の統合バイパス**: Pre-reply Analyzer による統合判定（`intent_info["research_decision"]`）が存在し、かつ困りごと・意見募集の調査保護フラグ（`consultation_flag`）が立っていない場合、`REPLY_RESEARCH_DECISION_MODEL` の単独呼び出しを行わず統合判定を採用して二重推論を解消する。`intent_info["research_decision"]` が未設定または `None` の場合は従来経路へフォールバックする。
    - **感情LLM採点の間引き**: `EMOTION_LLM_SCORE_INTERVAL`（デフォルト3）および `EMOTION_LLM_SCORE_SKIP_PEAK`（デフォルト0.20）により、一定間隔かつピーク感情変化が閾値以上の場合のみ Phase 2 LLM 採点を実行し、それ以外は Phase 1 ヒューリスティック採点のみで済ませる。
    - **所感生成の間引き**: `_ensure_background_llm_serialization` 内の `_serialized_update_thought` において、`BG_THOUGHT_UPDATE_INTERVAL`（デフォルト2）によるゲートを配備し、所感生成の実行間隔を制御する。
    - **呼び出し量の可視化ログ**: 1ターンの返信処理完了時に `[LLM_CALL_BUDGET] turn calls: analyzer=X research_decision=Y emotion_score=Z main=A memory=B thought=C` を出力する。

33. **【単一プロセス起動ロックのOS互換化仕様（Windows / Unix）】**:
    - **ロック取得関数（`_acquire_single_instance_lock`）**:
      - 一時ディレクトリ取得には `tempfile.gettempdir()` を使用し、`/tmp` への直書きを禁止。
      - Unix環境では `fcntl.flock`（`LOCK_EX | LOCK_NB`）、Windows環境では `msvcrt.locking`（`LK_NBLCK`）を使用して排他制御を実施。
      - いずれも利用不可能な環境では警告ログを出力し、例外終了させずに `fd` を返して続行する安全なフォールバック機構を備えること。
    - **二重起動抑止ライフサイクル**:
      - `main.py` のエントリポイントにおいて、起動ロック取得失敗時（二重起動検知時）は標準エラー出力にエラーメッセージを出力し、ステータスコード `sys.exit(1)` で安全に終了すること。

34. **【背景タスク例外の集計レポーター組み込みと管理者DM通知仕様】**:
    - **共通例外レポーター（`lib.error_report`）連携**:
      - `events.py`（受動的長期記憶抽出・感情更新・キューワーカー・タスク完了コールバック）、`research_events.py`（リサーチディスパッチ・プロビジョナル返信・バックグラウンド要約タスク）、`reply_core.py`（所感生成・エピソード保存・長期記憶抽出・チャンネル要約・内省タスク）等の背景タスクにおける `except` 節および done callback に `report_error(site, exc)` を組み込み、サイレントな例外握りつぶしを防止する。
      - 既存の例外制御フロー（握りつぶし、リトライ、フォールバック）は変更せず、既存のログ出力直後に `report_error` を呼び出す。
      - `lib.error_report` 未導入環境や単体テスト環境を考慮し、安全な `try-import`（`except ImportError: def report_error(site: str, exc: BaseException) -> None: pass`）を適用する。
    - **管理者DM通知ハンドラ（`front/discord/cog.py`）**:
      - `OllamaChatCog.__init__` の末尾において、`get_error_reporter().set_notifier(_discord_error_notifier)` により通知コールバックを登録する。
      - 集計閾値（`ERROR_REPORT_NOTIFY_THRESHOLD`）到達時に、管理者ユーザーID（`self.admin_user_ids` または `BOT_ADMIN_USER_IDS` / `ADMIN_USER_IDS`）へ `⚠️ **[Discord AI Bot 障害検知]**` メッセージを非同期タスク（`asyncio.create_task`）経由でDM送信する。

35. **【送信前意味検査・苛立ちクールダウン・文脈圧縮・訂正学習記録の統合仕様（BAN-139, BAN-143）】**:
    - **送信前意味検査（`front/discord/reply/semantic_guard.py`）**:
      - `assess_reply_risk` により、今ターンの訂正有無（`turn_has_user_correction`）、否定済みレジストリ（`denied_claims`）の存在および部分文字列即時一致（8文字以上）、直前Bot返信との類似度（`similarity >= 0.35`）を検出し、危険信号を判定。
      - リスク検知時は中位モデル（`inspect_reply_semantics`）により、①否定済み主張の言い換え再主張（`reasserts_denied`）、②主語・立場の取り違え（`subject_swap`）、③直近ユーザー発言との矛盾（`contradicts_latest_user`）をJSONで厳密検証。
      - 違反時は再考注記（`[再考注記: ...]`）を付与して1回だけ再生成。再生成後も違反の場合は安全にリアクションフォールバック（絵文字リアクション付加、本文空送信）へ切り替える。
    - **苛立ち検出とクールダウン制御（`front/discord/common/frustration.py`）**:
      - 怒りマーカー（💢、感嘆符3連以上、語気荒い表現等）を計数し、10分以内のユーザーシグナル履歴（訂正回数、breakdown連続回数、怒りスコア）を追跡。
      - 訂正回数2回以上、怒りスコア4以上、または2回連続breakdown検知時にクールダウン（デフォルト600秒）を開始。
      - クールダウン発動中は、`events.py` において非メンション通常発言を自動抑制（`reason=frustration_cooldown`）し、ユーザーの苛立ち・泥沼化を未然に防止（メンション時は返信維持）。
    - **短文化謝罪（泥沼化防止）**:
      - 訂正または怒りマーカー検知時に、Bot返信が80文字超の長文謝罪・的外れな解説になっている場合、簡潔な短文謝罪（60文字以内）へ整形し、泥沼化・不毛な謝罪ループを遮断。
    - **文脈注釈と要約圧縮（`front/discord/reply/context_compact.py`）**:
      - 会話履歴読み込み時に `annotate_context_lines` を介して否定済み発言に注記（`（※この内容はユーザーが否定済みです）`）を付与。
      - 履歴行数が閾値（`CONTEXT_COMPACT_TRIGGER_LINES`=40）を超過した場合、古い履歴を要約マーカー（`（これより前のやり取りは要約に圧縮されています）`）に畳み、最新 `CONTEXT_COMPACT_KEEP_RECENT`（12行）のみを保持して長大な生ログによる前提汚染を防止。
    - **訂正学習フィードバックと状態更新（`_record_correction_feedback_if_needed`）**:
      - ユーザー訂正発生時、`memory_store.add_feedback_item(..., feedback_type="user_correction")` を呼び出して学習データとして蓄積。
      - `memory_store.get_conversation_state` / `set_conversation_state` により、同一claimについて1時間以内の重複登録を抑止。
      - 同一claimが3回以上繰り返された場合は `[CORRECTION_REPEAT]` 警告ログを出力し、否定済みレジストリ（`register_denied_claim`）にも自動登録。

36. **【固有名詞グラウンディング検査と再考リトライ・文除去サニタイズ仕様】**:
    - **会話履歴・コンテキストの語彙抽出と許可集合構築**:
      - 返信生成パイプライン（`_generate_normal_reply`）において、ユーザー発言（`runtime.original_user_text`, `runtime.effective_user_text`）、会話履歴（`runtime.history`, `runtime.context_lines`）、およびBot名から英数字・カタカナ・漢字トークン（`[ァ-ヶーa-zA-Z0-9_\u4e00-\u9fff]{2,}`）を走査し、`allowed_context_terms`（許可語彙集合）を構築。
    - **未グラウンディング人名・捏造比喩の検知（`find_ungrounded_person_names`）**:
      - `DISCORD_NAME_GROUNDING_ENABLED`（デフォルトTrue）のもと、モデル返信内の人名・固有名詞を検査。
      - 会話履歴に存在しない中黒連結フルネーム（例: `アダム・リゲイ`）や、比喩・人物文脈（「〜の二番煎じ」「〜選手」「〜監督」等）と共起する架空人名のハルシネーションを確実に検出。
    - **再考注記付きリトライと文除去サニタイズ（`strip_ungrounded_person_sentences`）**:
      - 違反検知時は、`[再考注記: 直前の返信候補には会話の文脈に存在しない人物名『...』が含まれていました。会話履歴に登場した単語だけを使うか、人名を出さずに描写だけで返信してください。「〜の二番煎じ」のような言い回しで新しい人名を作らないでください。]` をプロンプト末尾に付加して再生成リトライを実行。
      - 再生成後も未グラウンディング名が残存し、かつ `DISCORD_NAME_GROUNDING_STRIP_FALLBACK`（デフォルトTrue）が有効な場合、違反名を含む文のみを除去し、残りの安全な文章を採用。
    - **多重防御とモック・単体完結設計**:
      - `harness.agent.prompt_parser.parser.validation` および `cleaner` からのインポートを試み、harness側の実装が未完了な環境でも、ローカルフォールバック関数により単体でテスト・型検査および実行を100%完走できる設計を維持。

37. **【画像同定・検証・採点ビジョン関数および `/image_search` 刷新・`/image_find` 新設仕様（BAN-161, BAN-162, BAN-170）】**:
    - **ビジョン関数のライフサイクル（BAN-170厳守）**:
      - `describe_image_for_identification`: 同定専用プロンプトで被写体・画風・文字を構造化抽出。
      - `verify_image_candidates`: 対象画像と候補画像を2枚見比べ、同一対象かを固定JSON（`verdict`, `confidence`, `reason`）で判定。
      - `score_image_candidates`: ユーザーの希望クエリと候補画像の一致度を固定JSON（`match_score`, `reason`）で採点。
      - いずれの関数も、1回の呼び出しの中で全画像の推論を完了させ、成功・失敗・例外問わず `finally` で `release_ollama_model(target_model, timeout_sec=5.0)` を確実に呼び出して即座にアンロードすること。
    - **`/image_search` 刷新**:
      - `ImageSearchToolAdapter.search_image_unified` に `vision_describe`（`describe_image_for_identification`）と `vision_verify`（`verify_image_candidates`）をキーワード引数として配線。
      - レスポンスの `candidates`（上位3件）を Embed の「候補・出典」フィールドに追加表示（一致マーク: `match`=✅, `partial`=❓）。
    - **`/image_find` 新設**:
      - 言葉（自然言語）から画像を検索し、AIの目で選んで表示するスラッシュコマンド（`OllamaChatImageFindMixin`）。
      - `interaction.response.defer(thinking=True)` の即時呼び出し（BAN-161, BAN-162）。
      - 中位モデル（`middle-llm`）による Danbooru 英語タグ・補助キーワードの抽出。
      - `IMAGE_FIND_ALLOW_EXPLICIT` が偽の場合は `-rating:explicit` をタグに強制付加して Danbooru API を非同期検索。
      - 候補画像を `_download_bytes` で取得し Base64 化後、`score_image_candidates` で一括採点。
      - `match_score >= IMAGE_FIND_MIN_SCORE`（既定60）の合格候補を最大 `IMAGE_FIND_MAX_RESULTS`（既定5）件、点数降順で Embed 送信。合格0件時は最高点1件を「一番近い候補（未確認）」として正直に報告。

38. **【自己言及リコールにおける外部コンテンツ言及メモ保持規則】**:
    - 自発投稿（Xタイムライン等）の生成時、参照したコンテンツの要約（作者名・冒頭文）を `runtime.utterance_reference_note` に確実にセットすること。
    - メッセージ送信パイプライン（`_send_reply` ➜ `_store_bot_utterance`）を通じて `memory_store.add_bot_utterance` に言及メモを安全に引き渡し、後からの思い出し質問に正確に応答できるようにすること。

39. **【Discord発言・リアクション・X観察の台帳記録仕様（`add_bot_activity` 連携）】**:
    - **発言記録（`_store_bot_utterance`）**:
      - 通常返信・自発投稿送信後、`memory_store.add_bot_activity` を呼び出し、`front="discord"`, `kind="reply"`, `summary=cleaned_reply[:400]`, `detail=reference_note[:80]`（存在時）として記録。
    - **相槌リアクション記録（`_record_reaction_activity`）**:
      - 12文字以内の相槌リアクション変換時、対象ユーザー名と発言内容（最大80文字）をフォーマットしたサマリー（`{target_author}の発言『{target_content}』に{emoji}を付けた`）を `kind="reaction"` として記録。
    - **スコアリアクション・フォールバック絵文字記録（`events.py`, `reply_core.py`）**:
      - `events.py` の `_safe_add_reaction` 成功時および `reply_core.py` のセマンティックガードによる絵文字フォールバック（`fallback_emoji`）付与時にも同様に `kind="reaction"` で活動ログを記録。
    - **自発投稿向けX観察記録（`reply_core_break.py`）**:
      - 自発投稿生成時に閲覧・参照したXタイムライン投稿群（最大3件、各80文字）を `front="x"`, `kind="observed"` として記録。
    - **安全設計と例外非遮断**:
      - `ACTIVITY_LOG_ENABLED`（既定: True）により制御し、`memory_store` に該当メソッドが存在しない場合やAPI失敗時（例外発生時）は安全に握りつぶしてログ（debug）出力にとどめ、通常の返信送信やリアクション付与本体を絶対に阻害しない。

40. **【自発言キャッシュ記録統一・出来事メモ保存・行為者照合（意味ガード拡張）仕様】**:
    - **送信完了キャッシュの全文記録保証（`reply_core_sending.py`, `reply_core_runtime.py`）**:
      - 分割送信（`_split_reply_text`）が行われた場合でも、`channel_context_cache` には先頭チャンクのみではなく返信全文（`raw_reply`）が `_append_sent_message(sent, full_text=raw_reply)` により1アイテムとして確実に記録され、`content` および `line` に全文が反映される。
      - ゲートウェイ経由の二重記録は `events.py` の `if getattr(message.author, "bot", False): return` で防止されている。
    - **会話状態メモ（`conversation_states`）への出来事メモ追加（`reply_core_episode.py`）**:
      - `EVENT_MEMO_ENABLED`（既定: True）有効時、4ターンに1度（`CONVERSATION_STATE_UPDATE_INTERVAL`=4）の更新タイミングに相乗りし、`_maybe_update_conversation_state` により会話履歴から `recent_events`（最大5件、新しい順）を抽出して `conversation_states` テーブルに永続化する。
      - スキーマ: `{"actor": "発言者名", "action": "したこと（短文）", "target": "相手または物", "at": "HH:MM"}`
      - 抽出規則: 会話履歴に明記された行為のみを、発言者名を履歴の表記のまま写して記録。履歴にない行為者の捏造を厳禁とし、Bot自身の行為も actor を Bot名として記録可能。
      - プロンプト構築時（`reply_core.py`）、DBから事前取得して `runtime.working_context["recent_events"]` に渡すことで、`builder.py` 経由で【直近の出来事メモ（行為者と行動）】がプロンプトへ確実に注入される。
    - **行為者照合と意味ガード拡張（`semantic_guard.py`, `reply_core.py`）**:
      - `ACTOR_CHECK_ENABLED`（既定: True）のもと、`assess_reply_risk` において返信文中に会話履歴の第三者（`current_speaker` と `bot_name` を除く参加者）が含まれ、かつ過去行為への帰属表現（`_ACTOR_ATTRIBUTION_RE`）が含まれる場合、`actor_attribution_risk` として `needs_check = True` を判定。
      - 中位モデル検査（`inspect_reply_semantics`）において、会話履歴（直近10行）と出来事メモ（`recent_events`）を提示し、`actor_mismatch`（言及した行為者が記録と食い違っているか）を検査。
      - 違反検知時は、出来事メモを引用した再考注記（`[再考注記: ... 行為者は出来事メモの通りにすること。確信が持てない場合は行為者の名を出さずに出来事だけに触れること。]`）を付与して1回再生成。再生成後も解消しない場合は相槌リアクション（`fallback_emoji = "🙏"`）へ安全にフォールバック。

41. **【送信前ペルソナ口調検査と再考注記リトライ・原文採用フォールバック仕様】**:
    - **送信前バリデーション順序の維持**:
      - `_generate_normal_reply` における送信前検査は、①意味検査（セマンティックガード）、②固有名詞グラウンディング、③ペルソナ口調検査、④短文化謝罪の4段階順序を厳格に維持する。
    - **口調検査（`find_persona_tone_loss`）**:
      - `DISCORD_PERSONA_TONE_CHECK_ENABLED`（デフォルトTrue）のもと、生成された返信文（`reply_content`）について、一人称（`BOT_FIRST_PERSON`）、免除文字数（`DISCORD_PERSONA_TONE_EXEMPT_MAX_CHARS`、デフォルト20文字）、および設定から取得したペルソナ語尾マーカー（`PERSONA_TONE_MARKERS` を `_resolve_persona_tone_markers` で正規化したタプル）を渡して語尾・一人称の喪失、掲示板常体、無個性な説明口調を検査する。
      - `PERSONA_TONE_MARKERS` がカンマ区切り文字列（例: `"んですよ, ですよ"`）またはリスト/タプルの場合は空白を除去して正規化し、未設定または空の場合は `None` として渡すことで、既定の組み込みマーカーへ安全にフォールバックさせる。
    - **再考注記リトライ**:
      - 違反検知時は、`[再考注記: 直前の返信候補はキャラクターの口調（語尾）が失われていました。掲示板の常体や無個性な説明口調ではなく、設定された一人称と語尾（例: 〜なんですよ…！）を使って、同じ内容をキャラクターらしく書き直してください。]` をプロンプト末尾に付加し、1回だけ再生成（`_call_reply_model_or_fallback`）を実行する。再生成後の検査にも `markers=tone_markers` を渡す。
    - **原文採用・警告ログフォールバック**:
      - 再生成後も口調が回復しなかった場合、口調は安全や事実性の問題ではないため会話を停止させず、「原文を採用し警告ログ（`[PERSONA_TONE] 再生成でも口調が回復しなかったため原文を採用: reply=...`）を残す」。
    - **例外非遮断と並行開発スタブ保護**:
      - 口調検査ブロック全体を `try...except Exception` で囲み、例外発生時もログ（warning）を残して返信フローを停止させない。
      - `harness.agent.prompt_parser.parser.validation` の実装が未完了でも単体動作するよう、ローカルのフォールバックスタブ関数（`def find_persona_tone_loss(..., markers: Iterable[str] | None = None) -> str | None: return None`）を備え、他サブプロジェクトの作業状況に依存せず単体でテスト・型検査および実行を完走できる設計を維持する。

42. **【直近の文脈画像へのオンデマンドキャプション注入仕様】**:
    - **プロセス内FIFOキャプションキャッシュ（`front/discord/common/message_media.py`）**:
      - プロセス内メモリのみ（永続化せず、再起動で消滅）、上限200件のFIFO管理（`_image_caption_cache`, `get_cached_image_caption`, `store_image_caption`）。
      - `build_message_media_context` で画像キャプション（`image_descriptions`）が得られた際、解決済みメッセージIDをキーに自動保存。
    - **文脈ターンへのオンデマンドキャプション注入（`front/discord/reply/reply_core_runtime.py`）**:
      - `_build_runtime` における文脈ターン（`merged_items`）確定後、`enrich_context_turns_with_image_captions` を呼び出し。
      - 対象（トリガー）メッセージ以外のターンから、`has_image=True` の最も新しいターンを1件特定。
      - キャッシュヒット時はターンの行末に ` [画像内容: {キャプション200字切り詰め}]` を即座に注入。
      - キャッシュミス時は `DISCORD_CONTEXT_IMAGE_CAPTION_ENABLED`（デフォルトTrue）が有効な場合に限り、`channel.fetch_message(id)` で実メッセージを取得し、最初の画像1枚のみを `describe_images_with_vision_model` でキャプションしてキャッシュ保存＋行末付与。
      - **1回の返信生成につきビジョン呼び出しは最大1枚**: トリガーメッセージ側に画像が存在する生成（`trigger_has_image=True`）では、文脈側のオンデマンドビジョン呼び出しをスキップし、GPU VRAMの載せ替え競合を防止。
      - 失敗（メッセージ取得不可・キャプション空・例外）はすべて安全に握りつぶしてログ（debug）にとどめ、通常の返信生成を阻害しない。

---

## 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/discord/tests ollama_bot/tests/test_reply_classifier.py
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front/discord
```
