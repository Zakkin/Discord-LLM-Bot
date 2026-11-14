# 🌸 Front Hutaba サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`front/hutaba/`）は、**ふたば☆ちゃんねる（`*.2chan.net`）のスレッド取得・Shift_JIS/HTML解析・レス投稿I/O・スレ監視・そうだねフィードバック学習**を担当する独立サブプロジェクトです。
プロンプト生成責務を持たず、掲示板I/Oおよびフロント固有サニタイズに専念する「純粋なフロント・I/O層」として動作します。

---

## 🎯 担当範囲と責務
1. **クライアント・スクレイピング・投稿基盤（`client/`）**:
   - `img2chan.py`, `img2channet.py`, `img2channet_parts/` による Shift_JIS HTML 解析、カタログ上位取得（`fetch_img_top5_threads` の `server="img" | "may"` 両対応）、消滅予定時刻解析（`parse_hutaba_thread_expiration` による `HH:MM頃消えます` / 古いスレ警告の抽出）、投稿文整形・サニタイズ（`sanitize_img_post_text` 等）、引用解決、Chrome MCP / HTTP フォームによるレス送信。
   - **プロンプト生成基盤のハーネス委譲:** プロンプト構築ロジック（`build_img_thread_post_prompt`, `build_img_thread_followup_post_prompt`, `build_img_thread_post_system_prompt`、書き込み後感情プロンプト `build_hutaba_post_emotion_prompt` 等）の正本は `harness/agent/prompt_parser/prompt_manager/board_prompts.py` に完全委譲・集約されており、`client/img2channet_parts/20_post_text.py` では薄いシムとして再エクスポートのみを提供。
2. **自律投稿・スレ監視・そうだね学習（`monitor/`）**:
   - `img2chan*.py` による定期/自律レス投稿、気分（感情状態 `mood`, `dominant_emotion`, `curiosity`, `excitement`, `fatigue` 等）に基づく `img` / `may` 巡回サーバー選定（`_select_hutaba_patrol_board`）と自発スレ巡回・選定（`_img2chan_proactive_post_loop`、候補枯渇時の他方サーバー自動フォールバック）、10分以内消滅スレ自動除外（`_is_hutaba_thread_expiring_soon`）、スレッド完走ケアと荒らし早期撤退（Vandalism Detection）、書き込み後のLLM動的感情・理由作文（`_generate_hutaba_post_emotion_via_llm`）、そうだね数推移の記録と学習、感情連動（Discord Presence/Bio同期）、追撃・相槌スケジューリング。
3. **スレッド要約・調査ツール提供（`thread_reader.py`, `tools.py`, `utils.py`, `adapter.py`）**:
   - `img_top5_dispatch`, `img_thread_dispatch`、直接返信テンプレート生成、URL 正規化、`HutabaAdapter`。

---

## 🏛️ コア機能・アーキテクチャ仕様

### 1. 自発スレッド巡回・選定機能 ＆ 気分によるimg/may選定 ＆ 10分以内消滅スレ・配信実況・リクエスト除外フィルター
- `HUTABA_PROACTIVE_POST_ENABLED`（既定 True）に基づき、`_img2chan_proactive_post_loop` がバックグラウンドでカタログ（`fetch_img_top5_threads`）を定期巡回。
- **気分（感情状態）に基づく巡回先選定 (`_select_hutaba_patrol_board`):**
  - 現在の感情状態（`mood`, `dominant_emotion`, `curiosity`, `excitement`, `fatigue`, `loneliness` 等）および同一サーバー連続巡回補正（飽き防止）に基づき、`may.2chan.net`（賑やか・刺激・雑多志向）と `img.2chan.net`（落ち着いたゆるい空気志向）の確率を動的計算して巡回先を決定。
  - 選定されたサーバーで有効な候補スレッドが0件だった場合、自動的にもう一方のサーバーへフォールバック巡回を実施。
- **フィルタリング条件:**
  - 直近書込除外（TTL 3600秒）、荒らしブラックリスト除外。
  - **終盤スレッド除外 (`HUTABA_MAX_PROACTIVE_THREAD_RES_COUNT`、既定 650件):** 新規参入時のスレッドレス数が 650件 を超えているスレッドは、書き込み直後に落ちるリスクや文脈固定化を防ぐため自発新規選定から自動除外（既存監視中スレの完走ケア上限 850 とは分離）。
  - **10分以内消滅スレ除外 (`_is_hutaba_thread_expiring_soon`, `HUTABA_MIN_THREAD_REMAINING_SEC=600`):**
    - `parse_hutaba_thread_expiration` により `HH:MM頃消えます` や `このスレは古いので、もうすぐ消えます。` の警告文を解析。
    - 残り時間が10分（既定600秒）以内のスレッドは、書き込み直後に落ちる無駄を防止するため自発選定および自発書き込み直前から自動除外。
  - **配信・実況スレ除外（`HUTABA_EXCLUDE_STREAM_THREADS`、既定 True）:**
    - リアルタイム映像・音声の視聴前提スレは文脈追随が不可能でハルシネーション・キャラ崩れの原因となるため自発選定から自動除外（`_is_img2chan_stream_or_live_thread`）。
    - URL検知: YouTube（`watch?v=`, `live/`, `shorts/`, `youtu.be/`）、Twitch、ニコニコ生放送、ツイキャス、SHOWROOM、OPENREC、および jump.php による外部リンク。
    - キーワード検知: タイトルや本文冒頭80文字の生配信、実況、歌枠、耐久配信、飲酒配信、同時視聴、歌枠リレー、建てるぜ、次スレ、避難所、本スレ等。
  - **リクエスト・お題募集スレ除外（`_is_img2chan_request_thread`、`HUTABA_EXCLUDE_REQUEST_THREADS=True`）:**
    - AIイラスト生成、お絵描きリクエスト、コラネタ募集等のスレッドは特定の制作・作業目的であり雑談に馴染まないため自発選定および自発書き込み直前から自動除外。
    - キーワード・構文検知: 【〜リクエスト〜】、AI/イラスト/お絵描き/手描き/絵/コラ ＋ リクエスト/リク、リクエスト/リク ＋ スレ/受付/募集/会場/所/部屋、リクエスト/リク ＋ どうぞ/ある/あれば/ください/待ってます/お願いします、お題/ネタ ＋ 募集/リクエスト等。
- ペルソナ・興味に基づいて最適なスレッドを1件選定し、初回レスを投稿してセッションを開始。

### 2. スレッド完走ケアと荒らし・不毛スレ早期撤退機構
- **完走ケア:** スレがdat落ち・消滅（`_img2chan_thread_dropped`）するまで監視セッションを維持。スレが落ちた時点で `status = "completed"` として正常終了。
- **自発的相槌・追撃 ＆ 被返信レス限定選定アルゴリズム（誰の返信もついてないレスへの返信完全防止）:**
  - 引用返信がない場合でも、一定クールダウン経過かつ会話進行時に自然な相槌・追撃レスを検討。
  - **被返信レス限定選定 (`_maybe_schedule_img2chan_autonomous_chitchat`, `calculate_img_thread_reply_counts`):**
    - 単に直近の独り言レスに飛びつくのを防ぐため、スレッド内の全レスから引用（`>`）およびアンカー（`>>`）による被返信数を集計（`calculate_img_thread_reply_counts`）。
    - 他者から既に返信をもらっているレス（被返信数 > 0）のみをターゲット候補として抽出し、最もホットなレスを選定（`_select_img2chan_chitchat_target`、同数の場合は新着優先）。
    - **誰の返信もついていないレスしかない場合は相槌タスクを確実にスキップ（見送り）**し、誰の返信もついてないレスへの返信を完全に遮断。
  - **フォールバック引用推定での誰の返信もついてないレスの完全除外 (`_infer_fallback_quote_post_no`):**
    - 語彙重複があっても誰からも返信されていないレス（`reply_counts <= 0`）は即座に対象外として除外し、単独書き込み（返信なしタイプ）を確実に維持。
- **荒らし検知（Vandalism Detection）による早期撤退:**
  - 攻撃的表現・差別表現の急増（30%以上）、同一文連投スクリプト、意味不明な記号爆撃を検知した場合は直ちに `status = "aborted"` をセットし、返信タスクをキャンセルして撤退。
  - 当該URLをブラックリストへ登録し、不毛な書き込みを防止。

### 3. Discordユーザー返信最優先（Yield）＆ 返信キュー・前処理待機完全連携 ＆ 中断・途中再開機構（Stateful Resumption）
- **包括的ビジー判定と完全譲歩待機 (`_is_discord_user_reply_busy`, `_wait_for_discord_user_replies_idle`):**
  - 単なる `_active_user_reply_count > 0` だけでなく、ターン待機中（`_active_user_reply_turn_count > 0`）、返信キュー待機中（`_reply_queue.qsize() > 0`）、および Discord 側のビジー判定メソッド（`_is_user_reply_busy()`）を包括的にチェック。
  - キュー待機中やWeb検索・前処理中も含め、Discord側が完全にアイドルになるまで待機・譲歩し、スレッド選定・スレ画Vision推論・レス生成によるDiscord返信遅延を完全根絶。
  - 起動直後の初期遅延（`HUTABA_PROACTIVE_POST_INITIAL_DELAY_SEC`）の既定値も 120.0秒 に引き上げ。
- 自発選定前、スレッドフェッチ前、返信生成前、投稿待機中に必ず `await self._yield_to_user_replies()`（内部で `_wait_for_discord_user_replies_idle` 実行）を呼び出し、Discord側のユーザー応答速度を最優先（Preemption）。
- 中断時には `thread_state` にチェックポイント（対象レス番号・アクション）を退避し、Discord返信完了後に最新差分を取得してスムーズに途中再開。

### 4. Discord返信生成機構の流用 ＆ 匿名ユーザー（としあき）対応 ＆ 中国語・簡体字サニタイズ
- スレッド履歴をターン構造へマッピング（OP=初回発言、既存レス=会話履歴、引用元=最新発言）し、Discordの返信パイプライン（`_call_reply_model_or_fallback`）を流用。
- 匿名性保護: `author_id="toshiaki"` 固定。特定個人名（「○○さん」等）の捏造をサニタイズ（`_sanitize_hutaba_reply_text`）で完全排除。思考タグ（`<think>...</think>`）を除去。エピソード記憶として保存。
- **中国語・簡体字混入サニタイズ (`_sanitize_img_post_chinese_leak`, `sanitize_img_post_text`, `sanitize_img_post_submission_text`):**
  - `劲儿`（直前が和文なら`っぷり`、単独なら`気合い`）、`为什么`（`どうして`）、`已经`（`すでに`）、`一点儿`（`少し`）等の語彙置換、および `劲`→`気`、`试`→`試`、`这`→`この`、`们`→`たち`、`欢`→`歓` 等の簡体字マッピングを実施し、不自然な中国語漏洩を水際で自然な日本語に変換・サニタイズ。英単語（`ComfyUIのLoRA` 等）や保護URLはそのまま維持。

### 5. 感情値変動とDiscord概要欄（Presence/Bio）連動 ＆ LLM動的感情理由作文
- スレ書き込み時および「そうだね」獲得時に感情値を更新。
- **スレッド書き込み成功時のLLM動的感情理由作文 (`_generate_hutaba_post_emotion_via_llm`):**
  - 固定文字列（「ふたば（img）でとしあきと雑談した」）や定型テンプレ文による更新を完全廃止。
  - スレッドの話題、返信先のとしあきの発言、自分が実際に書き込んだレス本文に基づき、LLM（`HUTABA_POST_EMOTION_SCHEMA`）自身が内的感情（dominant_emotion、scores）とその感情になった具体的な理由（`reason`、句点終わり1文）を動的に作文・反映。
  - LLM呼び出し失敗時や未接続時は、フォールバックとして `_build_hutaba_post_emotion_reason`（`img`/`may` 両対応）による文脈ベースの動的理由テキストを適用。
  - 前回の感情の残り火による主要感情（dominant）の食い違い（BAN-144）を防止するため、既存スコアに自然減衰（`decay_factor = 0.75`）を適用した上で感情スコアをマージ・正規化。
- 感情更新後に `_sync_emotion_presence(force=True)` を実行し、DiscordのPresence/Bioへ即座に反映。

### 6. ログ接頭辞 `[hutaba]` の統一 ＆ 投稿発言内容のログ記録 ＆ 全フロント共通行動ログ連携
- ロガー名を `logging.getLogger("front.hutaba")` に統一。
- ふたば関連の全ログ出力先頭に `[hutaba] ` 接頭辞を付与。
- **投稿発言内容の記録:** レス生成完了時（`レス文生成完了`）、書き込み実行開始時（`スレッド書き込み開始`）、成功時（`スレッド書き込み成功`）、失敗時（`スレッド書き込み失敗`）、およびスレッド反映確認時（`スレッド書き込み反映確認`）において、発言内容（`comment`）および対象URL・投稿番号を確実にログ（`INFO`/`WARN`）へ記録。
- **全フロント共通行動ログへの記録:** スレッド書き込み成功時（`success=True`）、`ACTIVITY_LOG_ENABLED`（既定 `True`）に基づき `memory_store.add_bot_activity` を呼び出し、`front="futaba", kind="post", summary=comment[:400], source_ref="{board}/{thread_id}"`（レス先がある場合は `detail="レス先: ..."`（最大80字））として行動台帳に記録。投稿失敗時は記録しない。

### 7. 自己連投・自己返信・自作自演の完全防止ガード
- **スレッド末尾の自己連投防止ガード (`_maybe_schedule_img2chan_autonomous_chitchat`):**
  - スレッドの最新レス（末尾）が自分自身（`own_post_nos` または `own_post_texts`）である場合は絶対に追撃・相槌を行わない（他人のレス待ち）。
  - 直近の自己投稿以降に他人の新着レスが1件も届いていない場合も即座にリターンし、一人連投・自作自演を遮断。
  - `HUTABA_AUTONOMOUS_CHITCHAT_ENABLED`（既定 `False`）による制御。
- **自己返信多重防御 (`_run_img2chan_followup_reply_task`):**
  - ターゲットレスの投稿番号（`post_no`）または本文（`text`）が自身の投稿履歴（`own_post_nos`, `own_post_texts`）に含まれる場合、タスク開始時に検知して即座に破棄。
- **引用返信候補からの自己レス完全除外 (`_discover_img2chan_quote_reply_candidates`):**
  - 投稿番号の一致判定に加え、本文（`reply_text`）が過去の自己投稿本文（`own_post_texts.values()`）と一致する場合も候補から除外。

### 8. プロンプト生成基盤のハーネス委譲と純粋なフロント・I/O層の維持
- 設計原則（principles.md 項目1.17）に基づき、プロンプト生成ロジック（`build_img_thread_post_prompt`, `build_img_thread_followup_post_prompt`, `build_img_thread_post_system_prompt` 等）の正本は `harness/agent/prompt_parser/prompt_manager/board_prompts.py` に完全集約。
- `front/hutaba/` はプロンプト構築ロジックを直接抱え込まず、薄いシム（再エクスポート）として後方互換性のみを担保。
- 本サブプロジェクトは、スレッド取得・Shift_JIS/HTML解析・レス投稿I/O・スレッド監視、およびフロント固有のサニタイズ（`sanitize_img_post_text` 等）に専念する「純粋なフロント・I/O層」としてのアーキテクチャ境界を厳格に維持。

### 9. ふたば返信生成への長期記憶（`memory.db`）接続 ＆ 長大スレッド序盤・中盤文脈保持
- **長期記憶・チャンネル要約接続:**
  - `_generate_hutaba_reply` において、`self.memory_store` を参照し `get_relevant_memories` を実行して `memory.db` から関連記憶およびチャンネル要約を動的取得。
  - スレッド単体（`channel_id=thread_no`）でヒットしない場合は `channel_id=None`（全体フォールバック）で過去の雑談・そうだね学習エピソードから最大3件の記憶を補完。
  - `sanitize_memory_candidates`（直前自己発言 `last_own_post_text` 考慮）を通してエコー汚染・会話ログ混入を防止。
- **長大スレッド序盤〜中盤文脈保持 (`_extract_early_thread_context_lines`):**
  - スレッド進行時（`len(posts) > 8`）に、直近8レス（`posts[-8:]`）で捨てられていた序盤〜中盤（`posts[:-8]`）から重要な文脈（OP前提、引用、疑問、主張・宣言、そうだね獲得レス、等間隔サンプリング）を最大6行抽出し、`early_context_lines` としてプロンプトへ注入。
  - プロンプト生成関数の新旧互換性のため、`TypeError` 発生時は `channel_summary` 先頭へ安全に結合する多重防御を配備。

### 10. 初回投稿時の引用先レス解決 ＆ オウム返し・自己投影ミラーリング検査と再試行
- **引用先レスの解決 (`_resolve_img2chan_quoted_target_reply`):**
  - 初回投稿時（`target_reply is None`）であっても、LLMが返した `quote_post_numbers` / `quote_reply_numbers`（JSON応答パース）、または直近レスとの語彙重複から自動補完された `inferred_post_no`（`_infer_fallback_quote_post_no`）に基づき、スレッド内の該当レス（辞書）を `effective_target_reply` として逆引き解決。
  - レス側の語彙がコメント内に含まれる部分一致判定（双方向サブストリング照合）により、複合名詞（「手順書」等）の自動補完漏れを防止。
- **オウム返し・自己投影ミラーリング検査の徹底 (`_img2chan_comment_parrot_reason`):**
  - `effective_target_reply` を対象として、`looks_like_img_reply_parrot` およびハーネス側の `looks_like_parrot_reply`（`_looks_like_self_projection_mirror_echo`）によるオウム返し検査を先行実行。
  - 相手の一部の行動や単語だけを拾って「私も毎回〜派なんですよ」「自分も〜するタイプです」と主語を自分にすり替えるだけの1文同調（自己投影ミラーリング・実質オウム返し）を確実に検知・棄却。
- **初回投稿の動的リトライループ (`anti_parrot`):**
  - 棄却時、初回投稿であっても `effective_target_reply` を元に `_build_img2chan_followup_retry_prompt`（相手の行動背景へのツッコミを促すプロンプト）を動的生成し、引用番号を引き継ぎつつ `anti_parrot` バリアントで再生成を実行。

### 11. スレッド内の相談・質問レス分類 ＆ 解決策Web検索最優先 ＆ 自己投影共感ミラーリング検知
- **相談・質問レスの事前抽出・分類 (`_classify_hutaba_reply_for_research`, `_find_hutaba_consultation_target_reply`):**
  - 初回レス生成時（`target_reply is None`）でも直近25件から相談・困りごと・意見募集レス（`is_consultation=True`）を末尾（新しい順）から自動抽出。
  - 画像ファイル名（`fu7481248.jpg`, `fu7481248`）や一般語ノイズ（`無限`, `時間`, `自分`, `途方`, `常駐` 等）を `_HUTABA_FILENAME_TOKEN_RE` / `_HUTABA_CONSULTATION_TERM_STOPWORDS` で除外し、高精度なトピック名詞を抽出。
- **解決策Web検索の最優先ディスパッチ (`_resolve_hutaba_thread_topic_context`):**
  - 相談・意見募集レスが検出された場合、OPのファイル名や雑多な単語ではなく、相談トピック＋代替・対策・方法クエリで `research_dispatch` を最優先実行。
  - 相手の困りごとに対する実用的な解決策ヒント（`>>483 解決ヒント調査`）をプロンプトへ注入し、長文丸写しや自己投影語りを抑止して実践的なアドバイスを生成。
- **自己投影共感ミラーリングの厳格検知 (`_img2chan_comment_parrot_reason`):**
  - 相談・困りごとレスに対して「私も〜探してて困ったことあるんですよ」「自分も〜で悩んだことある」と主語を自分に置き換えて同調するだけで具体的な解決策（`クロール`, `スクレイピング`, `チェック`, `確認`, `探すと`, `探ったら`, `残って`, `放置`, `ツール`, `スクリプト`, `拡張機能`, `通知`, `差分`, `巡回`, `代替`, `しか無い` 等）を含まない空虚な返信を `self_projection_sympathy_mirror`（BAN-168/BAN-171）として検知・棄却し、`anti_parrot` バリアントで再試行。
- **特定ペルソナ依存の排除と判定・正規化の汎用化 (`_SELF_PROJECTION_SYMPATHY_RE`, `_SIBLING_PERSONA_SUFFIX_RE`, `_normalize_for_sibling_plagiarism_check`):**
  - 掲示板監視共通ロジックに直書きされていた特定キャラ名や感嘆詞（「コイト」「ぴゃっ」等）を完全排除し、普遍的な日本語一人称（`私`, `わたし`, `あたし`, `僕`, `ぼく`, `俺`, `おれ`, `自分`, `うち`）および一般的語尾・フィラーによる正規化・検知へ汎用化。プロンプト追加指示や調査注記の例文も普遍的一人称へ統一。

### 12. 事前Web調査メモのノイズ棄却注記 ＆ メタ検索失敗レポート（辞書引き報告）の検知・再生成リトライ
- **事前調査メモのノイズ棄却注記付与 (`_resolve_hutaba_thread_topic_context`):**
  - スレッド主題・略称やとしあきの比喩・造語に対して事前調査を行った際、共起語由来の無関係な別作品・一般ニュースのノイズにLLMが引きずられるのを防ぐため、プロンプト注入テキストに「無関係な検索ノイズは無視すること」「検索報告は行わないこと」の注記（`_research_memo_note`）を付与。
  - キャッシュ（`thread_state["topic_research_summary"]`）には純粋な要約テキストを保持しつつ、相談・質問レス（`- 相談・意見募集レス` / `- 質問レス`）以外の主題事前調査メモ返却時にのみ注記を添える。
- **メタ検索失敗レポート（辞書引き報告）の検知・棄却 (`_img2chan_comment_parrot_reason`):**
  - 「調べてみたけど定義が確認できなかった」「〜のニュースばかり並んできちゃって」「特定の作品のネタですか？」といった、Web検索失敗や辞書解説をとしあきに報告してしまうレスを `_HUTABA_META_SEARCH_REPORT_RE` で検知。
  - `"meta_search_failure_report"` として棄却し、Bot素性の露呈や不自然なメタ会話を遮断。
- **リトライプロンプトの強化 (`_build_img2chan_followup_retry_prompt`):**
  - 棄却後の `anti_parrot` バリアント生成時、事前調査メモのノイズを無視し、検索失敗レポートを厳禁として前後のレスの流れに対する等身大のツッコミ・共感を生成するよう指示を強化。

### 13. 中盤レス全件保持（`all_replies`） ＆ マクロ話題抽出サマリー（`_extract_img2chan_macro_topic_summary`） ＆ Thoughtスキーマ導入 ＆ 空引用補完廃止
- **中盤レス全件保持 (`parse_img_thread_html`):**
  - 長大スレッドにおいて `cutoff` による中盤レスのドロップを防止するため、`result["all_replies"]` に全件レスリストを常時保持。スレッド全体の話題遷移・文脈を失わずに追跡。
- **マクロ話題抽出サマリー (`_extract_img2chan_macro_topic_summary`):**
  - 序盤（1/3）と中盤〜終盤（2/3）の頻出語を比較分析し、「スレッド初期の主題」と「中盤〜現在の盛り上がりトピック」を2層で要約抽出。
  - 話題転換（ラプチリオン等）が発生したスレッドでも現在の支配的トピックに即したレス生成を支援。
  - `build_img_thread_post_prompt` / `build_img_thread_followup_post_prompt` へ渡す際は `TypeError` ガードを配備し後方互換性を保証。
- **Thoughtスキーマ導入 ＆ 本文漏洩ガード:**
  - `IMG_THREAD_POST_COMMENT_SCHEMA` に `"thought"` フィールド（返信意図・境界整理用の思考メモ）を追加。
  - モデルが思考メモを本文（`comment`）に重複出力（漏洩）した場合は確実に棄却し、`anti_leak` プロンプトで再試行。
- **空引用時の強制引用補完廃止:**
  - モデルが `quote_post_numbers: []` を返した場合、わずかな語彙重複から無理に直近レスへ誤爆引用する `_infer_fallback_quote_post_no` の強制自動代入を廃止。
  - スレッド全体に対する独立した感想・発言として安全に投稿。

### 14. 誰の返信もついてないレスへの引用剥奪ガード ＆ 返信なしタイプのレス生成維持（初期スレッド用）
- **水際サニタイズ (`_generate_hutaba_reply`):**
  - 10レス未満の初期スレッドにおいて、モデルが自発的に `quote_post_numbers` や `quote_reply_numbers` を指定してきた場合でも、対象レスがスレッド内で誰からも返信されていない（`reply_counts <= 0`）レスであれば、引用指定を解除（空配列化）。
  - 初期スレにおいて「返信しないタイプのレス（本文のみの独立発言）」として安全に投稿させ、誰の返信もついていないレスに絡みに行く事故を水際で防止。

### 15. 被返信判定のキー正規化 ＆ 会話ツリー解決による親レス自動補正（Conversation Tree Resolution）
- **キー全形式登録と正規化照合 (`calculate_img_thread_reply_counts`, `_generate_hutaba_reply`):**
  - `calculate_img_thread_reply_counts` において、`norm_no`（数字のみ）、`No.{norm_no}`（プレフィックス付き）、レス番号 `number`、および元の文字列キーの全形式をカウント辞書のキーとして登録し、モデルの出力形式揺れ（`"No.1484064607"` や `"1484064607"`）による被返信判定ミス・引用剥奪を根絶。
- **会話ツリー親レス自動補正 (Conversation Tree Resolution):**
  - モデルが末尾や未返信レス（誰からも返信されていないレス）を指定した場合でも、そのレスが引用している親レス（被返信ありの話題の起点）を探索（`find_img_thread_quoted_posts`）。
  - 親レスが存在し被返信数 > 0 であれば親レスの投稿番号へ自動補正して引用を維持し、会話の流れを断ち切らずに安全に会話ツリーへ合流。

### 16. 被返信レス事前確定 ＆ 引用剥奪の完全廃止と最多被返信レス強制合流（7重防御）

> [!IMPORTANT]
> **【ふたば引用ルールの決定版仕様表】（セクション14, 15, 16, 25の統合仕様）**:
> ふたばスレッドへの投稿における引用（`quote_post_numbers`）は、以下の3ケースに厳格に分岐します。エージェントは過去の過渡的記述に惑わされず、この仕様表に従ってください：
>
> | ケース | 条件 | 確定動作 | 根拠セクション |
> | :--- | :--- | :--- | :--- |
> | **ケースA: 個別返信** | `target_reply is not None` | **引用先不変性の死守:** 被引用数チェック・親レス探索・最多被返信合流を**一切実行せず**、指定された `target_reply.post_no` を100%固定して引用返信。新着ツッコミへの返信が親レスに乗っ取られるのを完全防止。 | セクション25 |
> | **ケースB: 進行中スレッド自発投稿** | `target_reply is None` かつ 10レス以上 | **最多被返信レス強制合流:** 事前に最多被返信レスを特定。LLMが孤立レスや空配列を出力した場合も、会話ツリー親レス解決または最多被返信レスへ強制合流して確実に1件の引用を成立。 | セクション15, 16 |
> | **ケースC: 初期スレッド自発投稿** | `target_reply is None` かつ 10レス未満 | **単独レス許容:** 誰からも返信されていないレスへの無理な引用は剥奪し、空配列 `[]` の本文のみ単独レス（独立発言）として投稿。 | セクション14 |

- **事前返信先確定ガード (`_execute_img2chan_thread_post`):**
  - 進行中スレッド（10レス以上）で `target_reply` が None の場合、LLM呼び出し前にスレ内の最多被返信レスを特定し、`effective_target_reply` として確定して `_generate_hutaba_reply` に渡す。
  - プロンプト生成および返信パイプラインが最初から最多被返信レスをターゲットとして認識し、文脈に沿った引用返信を成立させる。
- **事後引用剥奪コードの完全撤廃と最多被返信レスへの強制合流 (`_generate_hutaba_reply`):**
  - 進行中スレッド（10レス以上）において「孤立レスのため引用指定を解除」という剥奪処理（空配列化）を完全廃止。
  - LLMが孤立レスを指定した場合や空配列 `quote_post_numbers: []` を返した場合であっても、スレ内最多被返信レス（最も返信が集まっているホットなレス）へ自動で強制合流させ、確実に1件の引用（`quote_post_numbers`）を成立させる。
  - 10レス未満の初期スレッドのみ、引用なし（空配列）の単独投稿を許容。
- **初回注目レス引用時のプロンプト文脈正規化 (`_generate_hutaba_reply`):**
  - 初回巡回（`quoted_own_posts` が空）において、進行中スレッドの最多被返信レスや注目レスを `target_reply`（`effective_target_reply`）として会話に参加する場合、「あなたが前に書いたレス:\n(今回引用された自分のレスは特定できていない)」という虚偽の文脈が生成プロンプトに残るのを防止。
  - プロンプト文脈を「スレッド内で盛り上がっている以下の注目レスに対して、引用（>）をつけて会話に参加します。」および「返信対象の注目レス:\n」へ自動置換・正規化し、虚偽の過去レス文脈を完全排除して自然な会話参入を実現。

### 17. 対象レス周辺文脈（子レス・直前会話・過去ローカルミーム）の抽出 ＆ プロンプト注入 (`_extract_hutaba_target_context_lines`)
- **課題と背景:**
  - 進行中スレッドで過去の最多被返信レスが選定された際、直近10件しかプロンプトに載らないと対象レスが孤立し、前後関係や被返信レス（ツッコミ）が脱落して直近の無関係なレスと合成されたポエム（BAN-169）が発生するリスクを根絶。
- **3層文脈抽出アルゴリズム (`_extract_hutaba_target_context_lines`):**
  1. **対象レスへの返信（直系子レス / 他人のツッコミ）:**
     - 番号引用（`>386`, `>>386`, `>>No.1446302523`）は全引用行で判定しつつ、本文引用（`target_first_line in q`）は1重引用（`>` で始まり `>>` で始まらない行）のみで厳密に判定。2重引用を含む孫レス（例: `>>319...\n>もう2割くらい下がってる\nがっつり下がって欲しいが無理だろな`）が直接の子レスとして誤抽出されるのを防止。
     - ラベルを `[対象レスへの他人のツッコミ(重複・言い換え合体厳禁)]` として明示。
     - `calculate_img_thread_reply_counts` において `find_img_thread_quoted_posts` の `max_targets=1` に限定し、孫引用による世代遡り水増し加算を排除。
  2. **対象レスの直前レス（親文脈）:**
     - 対象レスが直近10件に含まれない場合、その直前の会話レス（`target_idx - 1`）を `[直前の会話]` として抽出し、発言のきっかけ・文脈を維持。
  3. **過去言及レス（ローカルミーム）:**
     - 対象レス内の重要語（`_extract_hutaba_named_terms`）を抽出し、直近10件を除外した過去ログからそうだね獲得または引用付きの関連レスを `[スレッド内関連レス]` として抽出。
- **プロンプト注入契約と後方互換性:**
  - `build_img_thread_followup_post_prompt` に `target_context_lines` を渡す。ハーネス側が未更新の場合も `TypeError` フォールバックにより安全に稼働。
- **`IMG_THREAD_POST_COMMENT_SCHEMA` の思考メモ強化:**
  - `thought` フィールドの description を更新し、Step B・Step C において他人の既存ツッコミや直近レスの回答・数字の語尾変えパクりおよびキメラ合体を厳禁とし、既出ツッコミを「使用禁止の切り口」として除外した別視点・理由の提示を義務付け。

### 18. 修辞的疑問の検索誤爆防止 ＆ Thoughtスキーマ大喜利戦略拡張 ＆ スレ内風刺・反復ミーム抽出
- **修辞的疑問・大喜利フリ除外ガード (`_classify_hutaba_reply_for_research`):**
  - 「〇〇はどう思う？」「〇〇の気持ち」「〇〇はどうすんだろ」等の第三者・著名人への修辞的疑問や大喜利・皮肉のフリを `_HUTABA_RHETORICAL_OPINION_RE` で検出し、解決策Web検索対象から除外（`is_consultation=False`）。
  - 「これどう思う？」「このPC構成どう思う？」「みんなはどう思う？」「としあきはどう思う？」等の通常相談・質問は否定後読みと助詞境界によって的確に維持しつつ、公的機関等の的外れなマジレス検索誤爆を防止。
- **Thoughtスキーマの大喜利・代弁戦略拡張 (`IMG_THREAD_POST_COMMENT_SCHEMA`):**
  - `thought` 定義を【思考ステップ】として体系化：
    1. 意図分類（純粋な相談・質問 / 皮肉・大喜利・修辞的疑問 / 通常の雑談・ツッコミ）
    2. 戦略決定（皮肉・大喜利・第三者への問いかけ時はメタ逃げマジレスを厳禁とし、[方針A] その人物になりきった尊大・無責任な本音代弁、または [方針B] スレ内の流行ミームや定型句での冷たい突き放しを整理）
    3. スタイル（大喜利ボケ投下時はペルソナ語尾よりも切れ味・オチを優先）
- **マクロ話題要約での風刺構図・反復ミーム抽出強化 (`_extract_img2chan_macro_topic_summary`):**
  - 抽象的な一般名詞のみならず、スレッド全域で反復言及される固有名詞・カタカナ語（いじり対象やキャラクター名、エンティティ）および複数レスで出現・引用される短文フレーズ（定型ツッコミ・スレ特有のノリ・ミーム）を汎用的な出現頻度・パターン解析によって抽出。
  - 特定ワードのハードコードを排した汎用ロジックにより、スレ内の風刺構図や流行フレーズをマクロ要約注記としてプロンプトへ注入し、スレッド特有の空気に即したレス生成を実現。

### 19. 先行会話スライス抽出・Thoughtスキーマ改修（クリティカル思考強制）・文脈保護・Hook Echo防御・略称同義語展開と直前無関係レス混入防止
- **課題と背景 (BAN-169, BAN-179):**
  - ふたばスレッド等において、対象レスの直前の先行レス（文脈・議論の実態）が脱落し、相手の美化された意見や単語を文頭で反復（Hook Echo）して「プロレスの面…！あの頃の熱さって〜」と浅いオウム返し＋全肯定ポエムに直行してしまう事故を根絶。
  - さらに、数レス前の話題（例: `NAI` や `LoRA`）を引き継いで別表記・略称（例: `novelai` や `Lola`）で書かれたレスに返信する際、過去のスレ内関連レスが抽出されず、直前に挟まった無関係な単発レスが先行レスとして混入する課題を解決。
- **直前会話スライス抽出の拡張と無関係別話題レスの混入防止 (`_extract_hutaba_target_context_lines`):**
  - 直前1レスのみの抽出から、対象レス直前の先行1〜3レス（`slice_start = max(0, target_idx - 3)`）を会話スライスとして抽出するよう拡張。ラベル `[直前の会話・先行レス]` を付与し、先行する議論の流れ・幻滅・ツッコミの文脈を確実にプロンプトへ注入。
  - **直前先行レスの無条件保持と2件以上前の無関係別話題レス除外:** 直前1件の先行レス（`target_idx - 1`）は直前の直系文脈である可能性が最も高いため、共通重要語がなくても無関係別話題判定から除外して絶対に保持する。対象レスに引用（`>`）がなく固有の重要語を持つ場合、2件以上遡るレス（`target_idx - 2` 以降）に挟まった「別の固有名詞を持ち、かつ対象レスの重要語を含まない」無関係な単発レス（例: AIの話題に挟まった時岡リメイク等）をスキップ・除外。
- **略称・表記揺れ同義語展開とスレッド内関連レス抽出 (`_extract_hutaba_named_terms`, `_extract_hutaba_target_context_lines`):**
  - `_HUTABA_TERM_SYNONYM_GROUPS` / `_HUTABA_TERM_SYNONYM_MAP` により、AIモデル・技術用語等の略称・表記揺れ同義語（`NAI` ⇔ `novelai`, `LoRA` ⇔ `Lola`, `ComfyUI` ⇔ `Comfy`, `SD` ⇔ `StableDiffusion`, `PixAI` ⇔ `ピクスAI` 等）を自動展開して検索キーワードに追加。
  - セクション3（`[スレッド内関連レス]`）において、`target_idx` より前の全レスを新しい順（逆順）に走査し、展開された重要語・同義語にマッチする先行言及レスを（引用やそうだね数に依存せず）確実に最大2件まで抽出して注入。
- **フォールバック時も含めた文脈破棄防止 (`_generate_hutaba_reply`):**
  - ハーネス側が `target_context_lines` を受け付けず `TypeError` が発生した場合でも、`fallback_parts` 内に `【対象レスの周辺文脈（直前の会話・他人のツッコミ）】` として `target_context_lines` をマージし、文脈情報を絶対に破棄しない安全設計を徹底。
- **Thoughtスキーマのクリティカル思考誘導改修 (`IMG_THREAD_POST_COMMENT_SCHEMA`):**
  - `thought` の description を3段階推論（Step A / Step B / Step C）へと改修：
    - **Step A（暗黙の文脈・先行話題の特定）:** 対象レスが反応している直前の話題・先行レス（何についての議論・幻滅・ツッコミか）を特定。
    - **Step B（主張の検証とクリティカルな別視点・カウンターの検討）:** 相手の主張を鵜呑みにせず、「本当にそうか？（美化されていないか？裏の実態・狂人・泥沼はどうだったか？）」と疑い、安易な同調ポエムを避けて「実態のシニカルな補足」「裏の真実の提示」「鋭いツッコミ」のカウンター方針を決定。
    - **Step C（表現のサニタイズ）:** 冒頭での相手単語の反復（「〇〇…！」等）や抽象ポエム（「あの頃の熱さ〜」等）を完全排除し、生々しい空気感や実態に踏み込んだ短い一言を設計。
- **文頭フレーズ反復（Hook Echo）＋抽象ポエム検知・棄却 (`_img2chan_comment_parrot_reason`):**
  - 相手レス内の単語を文頭トークンとして反復（`^{first_token}[…!！]+`）しつつ、具体性のない抽象ポエム表現（「熱さ」「懐かし」「青春」「時代」「あの頃」「確かに」「分かります」等）を含むレスを `hook_echo_abstract_poem` として検知・棄却し、再試行ループへと誘導。

### 20. スラング辞書連携による検索クエリサニタイズ ＆ 用語注意メモ注入 ＆ 周辺レス内在化（メタ実況・キメラ疑問文の禁止）
- **スラング辞書による検索クエリトークンの除外 (`_classify_hutaba_reply_for_research`):**
  - 「すみフラ」「うろフラ」「フラ」（質問・相談の前置き略語）等のネットスラングが、質問・相談レスのトピック名詞抽出において誤ってWeb検索クエリに混入するのを防止するため、`filter_query_terms_by_slang` を適用してトークンから除外。無関係なノイズ検索（フラダンス等）を根本遮断。
- **スレッド用語・スラング注意メモのプロンプト注入 (`_generate_hutaba_reply`):**
  - 対象レス本文からスラング（`lookup_slang_in_text`）を検出し、プロンプトのコンテキスト（`【スレッド用語・スラング注意メモ】`）として注入。LLMが前置きスラング（「すみフラ」等）をキャラ名や作品名と誤認して頓珍漢な返答を生成するのを防止。
- **周辺レス・ツッコミの内在化義務と他人事メタ実況・キメラ疑問文の厳禁 (`_generate_hutaba_reply`, `_img2chan_comment_parrot_reason`):**
  - 周囲のツッコミや判定結果を「〜ってツッコミが来てますけど」と他人事としてメタ実況・報告したり、周囲の単語を適当に繋ぎ合わせたキメラ疑問文（「〜なんですかね？」）を生成することをプロンプト指示およびリトライ指示で厳禁。
  - レス生成時、周囲のツッコミをスレッドの実情・前提知識として内在化し、自分自身のスタンス・結論ファースト（難易度やアプローチ等）でズバッとアドバイスするよう指示。
  - 出力文にメタ実況表現が含まれていた場合、`_img2chan_comment_parrot_reason` で `meta_commentary_report` として即座に棄却・再試行。
- **既存レス（子レス・周辺レス）の語尾変えパクり・キメラ合体検知 (`_img2chan_comment_parrot_reason`: `sibling_reply_plagiarism`):**
  - スレッド内の他人の既存レス（子レスや周辺レス）の本文をペルソナ口調に言い換えただけの単一パクり（共通正規化コア7文字以上、または5文字以上かつ2-gram重複率0.65以上）や、複数の他人のレスからフレーズを拾って繋ぎ合わせたキメラ合体（2件以上の既存レスと共通コア5文字以上または重複率0.55以上）を水際で検知・棄却。
  - **話題語誤検知防止と固有2-gram照合:** `_normalize_for_sibling_plagiarism_check` での文字クラス誤用によるひらがな無差別削除を是正し、一人称・定型語尾・記号の境界付き正規化へ刷新。2-gram重複率計算では対象レス・OPの共通話題語（「モザイク」等のトピック語）の2-gramを除外した固有表現同士で比較し、スレッド全体の話題語を含む正当なリアクションの誤棄却を根絶。8文字未満の極短レス断片は2-gram重複率比較から除外して偶然一致を防止。
  - プロンプト指示（`meta_commentary_guidance`）およびリトライ指示（`_build_img2chan_followup_retry_prompt`）において、他人が既に書いたツッコミや回答を「使用禁止の切り口」として除外し、まだ他人が言っていない独自の別視点・理由・リアクションを提示することを徹底。
  - **兄弟レス盗用棄却時の観測メタデータ記録 (`ParrotReason`):** 兄弟レス盗用判定時に、過剰検出か正当棄却かを事後検証できるよう、一致した兄弟レス番号リスト（`matched_sibling_post_nos`）および最長共通部分文字列の長さ（`longest_common_subsequence_len`）を保持する `ParrotReason(str)` を返却。棄却ログを `[hutaba] オウム返し候補のため棄却: model=%s url=%s reason=%s matched=%s lcs_len=%s comment=%r` へ拡張し、単一レス一致・キメラ合体の一致情報を詳細に記録。既存の文字列比較や真偽値判定との後方互換性を100%維持。
  - **定着用語境界文（言い換え禁止の拡大解釈抑止）:** スキーマ（`IMG_THREAD_POST_COMMENT_SCHEMA` Step B）、常設プロンプト（`meta_commentary_guidance`）、リトライ指示（`_build_img2chan_followup_retry_prompt`）に「ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。」の境界文を明記。既出ツッコミの言い換え禁止を用語そのものの使用禁止と誤解して生じる用語崩れ（「クリーンイン」等）を防止。
- **非アンカー引用行（>本文）の先行サニタイズ除去 (`_sanitize_hutaba_reply_text`):**
  - LLMがcommentフィールド内に `>対象レス本文` のような非アンカー本文引用行を出力した場合、`extract_first_user_facing_reply` が先頭の `>` だけを剥がして本文に混入させるのを防ぐため、通常の本文行が存在する場合は非アンカー引用行を先行除去。アンカー引用（`>>数字`）は先頭に確実に保持。
- **自発巡回初回投稿失敗時のゾンビ監視セッション防止 (`_run_img2chan_thread_post_after_delay`):**
  - 自発巡回において初回レス投稿が失敗または見送られた場合（`not posted`）、監視セッション（`_monitor_img2chan_thread_session`）を開始せずに即座にタスクを終了し、未投稿スレッドが無駄に監視され続けるリソース浪費を遮断。投稿失敗時も警告ログを確実に記録。
- **自発スレッド選定での短文高レス定時スレ除外 ＆ 直近被返信レス優先ターゲット固定 (`_select_proactive_thread_candidate`, `_execute_img2chan_thread_post`):**
  - カタログ選定時、タイトル・OPが8文字以下かつレス数200件以上の定時・実況スレッド（「むっ！」等）を通常候補から除外し、文脈が掴める日常雑談スレを優先。
  - 進行中スレの事前返信先選定において、直近20件以内の被返信レスを優先し、30レス以上前の古い1回被引用レスへの誤固定を防止（全体フォールバックは被引用数2件以上の盛り上がりレスに限定）。
- **検索ストップワード拡充とクエリ部分語重複排除 (`_HUTABA_TOPIC_STOPWORDS`, `_resolve_hutaba_thread_topic_context`):**
  - 一般語・作品一般概念（「キャラ」「アニメ」「原作」「性的」「エロ」「モザイク」「ファン」等）をストップワードに追加し、Web検索クエリ構築時に採用済み長語の部分文字列（例:「ファンタジア」に対する「ファン」）を重複排除。

### 21. スレ画（OP画像）のURL抽出・Qwen3-VL-8Bキャプション抽出およびVRAM即時解放規約・LRUキャッシュ機構 (項目170, 180準拠)
- **課題と背景:**
  - ふたば（`img.2chan.net`）は画像掲示板であり、スレ画（OP画像）がスレッド全体の前提・文脈となる。従来は本文のみを取得していたため、「（本文なし）」スレッドやコラ画像スレでAIが文脈を理解できず、的外れな要約やレス投稿を行う課題が存在した。
  - さらに、同一スレッドに対する初回取得時・書き込み反映確認時・監視ループ初回ポーリング時の短時間の間に、同一スレ画に対してVisionモデル（`media-model-01` / `qwen3-vl:8b`）の推論が3回連続で実行される重複処理課題を解決。
- **スレ画URL抽出 (`parse_img_thread_html`):**
  - OP画像リンク（`<a href="/b/src/....jpg"><img src="/b/thumb/....jpg"></a>`）からフル解像度URL（`op_image_url`）およびサムネイルURL（`op_thumb_url`）を高精度に正規表現抽出。
- **Visionモデルによるキャプション化とVRAM即時解放 (`caption_hutaba_thread_image`, 項目170, 180準拠):**
  - サムネイル画像をダウンロード（上限5MB）し、`Qwen3-VL-8B`（`OLLAMA_VISION_MODEL`）を用いて画像内の被写体・情景・文字起こしテキスト（客観的キャプション）を非同期生成。
  - **タイムアウト拡張とKVキャッシュ最適化:**
    - Cold Startによるモデルロード時間および画像推論時間に耐えるため、フォールバックタイムアウトを従来の15秒から `60.0` 秒（`OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC`、既定 `60.0` 秒）へと拡張。
    - Vision推論のKVキャッシュ最適化として、`call_ollama` に `num_ctx=num_ctx`（`OLLAMA_VISION_NUM_CTX`、既定 `4096`）を明示的に渡し、過大なコンテキスト確保によるVRAM圧迫やコンテキスト不足による推論失敗を防止。
  - 推論完了後（成功・失敗・例外問わず `finally:` 内）で必ず `release_ollama_model(target_model, timeout_sec=5.0)` を呼び出し、VRAMを0MBに即時解放（項目170, 180）。
- **画像URL単位のキャプションLRUキャッシュ ＆ 重複推論抑止 (`caption_hutaba_thread_image`, `fetch_img_thread`):**
  - `_THREAD_IMAGE_CAPTION_CACHE`（最大128件）により、同一画像URLに対するダウンロードおよびVision推論を透過的にスキップ（LRU順序維持）。
  - `fetch_img_thread(url, max_replies=30, timeout_sec=15.0, cached_caption=None)` にオプショナル引数 `cached_caption` を導入。指定時はVision推論を一切走らせずキャプションを即時設定＆キャッシュ同期。
  - スレッド監視ループ（`_monitor_img2chan_thread_session`）、投稿前フェッチ、および投稿後反映確認（`_execute_img2chan_thread_post`）において、`thread_state` 内の `op_image_caption` を再利用し、重複推論を完全に遮断。
- **要約・レス生成へのコンテキスト連携 (`thread_reader.py`, `img2chan_posting.py`):**
  - スレッド直接返信生成（`build_img_thread_direct_reply`）およびスレッド要約（`format_img_thread_summary`）において、OPテキストに `(スレ画: {op_caption})` を付与。
  - レス文生成（`_execute_img2chan_thread_post` / `_generate_hutaba_reply`）において、OP投稿のテキストへ `[スレ画内容: {op_caption}]` を注入し、画像内容を踏まえた高精度なレス・ツッコミを生成。

### 22. スレ画・画像特定レスに対する自律画像検索 (search_image) 連携 (項目181準拠)
- **課題と背景:**
  - 「これ誰？」「このキャラ誰？」「何のキャラ？」「元ネタ何？」といった画像特定質問レスが投稿された際、指示語中心で名詞トークンが抽出できず事前Web調査がスキップされ、スレ画のキャラクター・人物を特定できずに的外れな返答をしてしまう課題を解消。
- **画像特定質問の検知 (`_HUTABA_IMAGE_QUESTION_RE`, `_classify_hutaba_reply_for_research`):**
  - `_HUTABA_IMAGE_QUESTION_RE` および特定要求語判定により、「これ誰？」「何のキャラ？」「元ネタ何？」「画像検索」等の画像特定質問を `category="image_question"`, `is_image_question=True` として高精度に分類。
  - 「この子って通るんですか？」等の通常の疑問文を誤検知しないよう特定要求語（「誰」「何これ」「何のキャラ」「元ネタ」等）を検証。
- **スレ画特定画像検索の実行 (`_resolve_hutaba_thread_topic_context`):**
  - 対象レスが `is_image_question` またはOPが画像質問であり、かつスレ画URL（`op_image_url` または `op_thumb_url`）が存在する場合、`harness.agent.tool_registry.adapters.image_search.ImageSearchToolAdapter.search_image_unified` を非同期呼び出し（タイムアウト12.0秒）。
  - 未配備・インポート失敗時はスレ画キャプション・スレタイを用いたWeb検索ディスパッチへ多重防御フォールバック。
  - 検索結果を `スレ画特定調査メモ`（客観的情報、検索メタ報告禁止注記付き）としてプロンプトに注入。
- **スレッド要約・直接返信への特定結果反映 (`thread_reader.py`):**
  - `build_img_thread_direct_reply` において、`thread` 内に `op_image_search_summary` が存在する場合、スレ画キャプションに `(特定: {op_image_search_summary})` を付与して要約精度を強化。

### 23. may.2chan.net サポート ＆ 画像投稿（HTTPマルチパート upfile） (項目184準拠)
- **may.2chan.net の完全サポート:**
  - `_HUTABA_THREAD_URL_PATTERN`（`img` / `may`）により、`may.2chan.net/b/res/*.htm` を自動抽出・正規化（`normalize_img_thread_url`）。
  - `_get_hutaba_cache_token_url(server)` により、各サーバーに応じたキャッシュトークン（`https://may.2chan.net/bin/cachemt7.php`）を安全に取得。
  - レス投稿時の `Origin` ヘッダー（`https://may.2chan.net`）および Referer をサーバーに合わせて動的生成。
- **画像投稿サポート（`submit_img_thread_reply`）:**
  - 引数 `image_path: str | Path | None = None`、`image_bytes: bytes | None = None`、`image_filename: str | None = None` をサポート。
  - 画像指定時は Chrome MCP がファイル選択非対応のため、即座に直接 HTTP 投稿（`_submit_img_thread_reply_via_http`）へ安全にフォールバック。
  - `aiohttp.FormData` のマルチパート形式（Shift_JIS対応）を用いて `upfile` フィールドに画像バイナリを添付して送信。

### 24. ユーザー指定スレッド重点巡回（Focus Patrol） ＆ 複数URL先頭限定 ＆ 自発巡回プリエンプション (項目184準拠)
- **複数URL時の先頭限定採用:**
  - Discord メッセージ内に複数のスレッドURLが含まれている場合、先頭の1件のみを確実に監視対象として採用し、残りはログ出力の上で安全にスキップ。
- **自発巡回スレッドの中断と最優先切り替え（Preemption）:**
  - ユーザー指定スレッドを検知した際、既存のアクティブタスクが自発巡回（`user_requested=False`）である場合は即座にタスクをキャンセル（`tasks[active_url].cancel()`）し、ユーザー指定スレッドへ即時切り替えて実行。
- **重点巡回モード（Focus Patrol）によるポーリング・相槌短縮:**
  - `thread_state["user_requested"] = True`, `thread_state["focus_mode"] = True` を設定。
  - 初回投稿遅延を短縮（`IMG2CHAN_USER_REQUESTED_POST_DELAY_SEC`、既定 `5.0` 秒）。
  - 監視ポーリング間隔を短縮（`IMG2CHAN_USER_REQUESTED_POLL_SEC`、既定 `15.0` 秒）。
  - 自発的相槌クールダウンを短縮（`IMG2CHAN_USER_REQUESTED_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC`、既定 `120.0` 秒）。
  - ユーザー指定スレッド稼働中は自発選定ループ（`_img2chan_proactive_post_loop`）を待機させ、ユーザー指定スレッドの対話を最優先。

### 25. あぷ小動画レス除外 ＆ 静止画自動キャプション認識 ＆ target_reply引用先不変性ガード (項目179, 184準拠)
- **課題と背景:**
  - あぷ小の動画ファイル（`fu\d+\.mp4`, `webm` 等）を含むレスに対し、中身が見えていないにもかかわらず周囲のテキストから知ったかぶりをしたハルシネーション返信をしてしまい、「AI糸。雑なコピペレスは止めな」と見破られる課題が発生。
  - さらに、上記ツッコミを入れた新着レス（被返信数0件）に対してBotが返信した際、孤立防止ロジックが無差別に発動して親レス（Bot自身の前レス）へ勝手に補正され、返信先がすり替わって二重引用されてしまうバグ（BAN-169）を根絶。
- **動画メディア（mp4, webm等）を含むレスの反応スキップガード (`_has_hutaba_unsupported_media`):**
  - `_HUTABA_VIDEO_MEDIA_RE` により、レス本文中の動画ファイル（`fu\d+\.(mp4|webm|avi|mov|mkv|flv)` や直接URL）を検知。
  - 自発相槌選定（`_select_img2chan_chitchat_target`）、自発追撃スケジュール（`_maybe_schedule_img2chan_autonomous_chitchat`）、事前返信先確定ガード（`_execute_img2chan_thread_post`）、および引用返信候補抽出（`_discover_img2chan_quote_reply_candidates`）の全経路で動画レスを候補から確実に除外。
- **あぷ小・あぷ静止画像の自動キャプション化とプロンプト注入 (`_extract_hutaba_attached_image_urls`, `_generate_hutaba_reply`):**
  - 対象レス本文からあぷ小（`fu\d+\.(jpg|png|webp)`）およびあぷ（`f\d+\.(jpg|png|webp)`）の画像URLを正規表現抽出。
  - `caption_hutaba_thread_image` を非同期呼び出し（最大2枚まで）して画像内容を認識し、プロンプトのコンテキスト（`【対象レスの添付画像（あぷ小）の解釈メモ】`）として注入。画像内容を踏まえた自然な返信を生成。
- **`target_reply` 指定時の引用先不変性ガード（新着レスの親補正防止）:**
  - `_generate_hutaba_reply` 内で、`target_reply is not None` の個別返信時は被返信数バリデーション（親レス探索・補正、最多被返信合流）を一切実行せず、`target_reply` のレス番号を100%固定。新着ツッコミレスが親レスにすり替わる事故を根本遮断。
- **相手の発言本文（非引用行）優先引用による二重引用防止 (`compose_img_thread_reply_comment`):**
  - 対象レスに前レス引用（`>...`）が含まれている場合、`_extract_img_post_body_lines` により相手自身の発言行（非引用行）を優先して引用し、前レスまで巻き込んだ不要な多重引用（`>>...`）を防止。

### 26. ふたばレス生成における思考先行・検索クエリ改善・直前文脈保護 (BAN-169, BAN-171準拠)
- **課題と背景:**
  - ふたばスレッドへの返信生成時、Web検索が「デカパイ 面白 無理 所他 故郷」という無関係なR18ノイズを取得し、ターゲットレスの直前文脈が過剰判定で破棄され、さらに JSON Schema の `thought` が空文字 `""` でスキップされてトートロジー返信が出力される課題を根本解決。
- **`IMG_THREAD_POST_COMMENT_SCHEMA` への `minLength` 制約追加:**
  - `"thought"` プロパティに `"minLength": 10`、`"comment"` プロパティに `"minLength": 1` を追加し、思考メモを空文字 `""` でバイパスして即座に本文を出力する推論のショートカットをスキーマレベルで遮断。3段階思考（文脈特定・主張検証カウンター・表現サニタイズ）を強制。
- **`think` オプションの設定尊重 (`_generate_hutaba_reply`):**
  - `monitor/img2chan_posting.py` 内の `think=False` ハードコードを解消し、`cfg_bool("IMG2CHAN_POST_THINK", cfg_bool("OLLAMA_MAIN_MODEL_THINK", False))` により環境変数・設定値を反映。
- **検索ノイズ防止用ストップワード拡充 (`_HUTABA_TOPIC_STOPWORDS`):**
  - 一般語（無理、面白、面白い、所他、刺客、警戒、結局、今更、困る 等）およびR18ノイズ語（デカパイ、巨乳、美乳、爆乳、おっぱい 等）を `_HUTABA_TOPIC_STOPWORDS` に追加し、頻出語抽出や事前Web調査クエリへの混入を遮断。
- **スレッド主題・作品ドメイン優先の検索クエリ生成 (`_resolve_hutaba_thread_topic_context`):**
  - 単なるスレ全体頻出語の寄せ集めを廃止し、スレッドのマクロ主題（`macro_topic_summary`）、OPテキスト/タイトル、スレ画キャプションから抽出した「作品・主題ドメイン語」を最優先でクエリ先頭（最大2語）に配置。
  - 次に対象レスの固有語、最後にスレ全体頻出語（最大4語まで）で補完する優先順位付きクエリ生成により、スレ主題から乖離した的外れな検索ノイズを防止。
- **直前会話スライスの直前1件無条件保持 (`_extract_hutaba_target_context_lines`):**
  - `target_idx - 1`（直前の1レス）は、共通語がなくても直前の直系文脈として無関係別話題判定から除外して絶対に保持。2件以上遡る場合のみ話題乖離判定を適用。

### 27. スレッド監視における苛立ち・否定検知と自律返信クールダウン連携 (BAN-139準拠)
- **課題と背景:**
  - ふたば（img/may）フロントにおいて、としあきから「違う」「そうじゃない」「いい加減にしろ」と否定やツッコミを受けている状況にもかかわらず、Botが空気の読めない自律レスを連投してスレを荒らしてしまう課題（BAN-139）を防止。
- **否定・苛立ちレス判定 (`_looks_like_hutaba_anger_or_denial`):**
  - `_HUTABA_ANGER_OR_DENIAL_PATTERNS` により、否定表現（ちがう、違う、そうじゃない、言ってない、言っとらん）、苛立ち・怒り表現（いい加減にしろ、ふざけんな、黙れ、うぜえ、ウザい、消えろ）、呆れツッコミ（なんなん、何言ってんだ、会話にならん、会話になってない）、および感嘆符連続（`[！!]{3,}`）を検知。
- **追従返信タスクでの見送り判定 (`_run_img2chan_followup_reply_task`):**
  - 直近5件のレス（`target_replies` または `thread_state["target_replies"]`）を検査し、苛立ち・否定表現が2件以上（`denial_hits >= 2`）検知された場合、返信タスクを直ちに中断・スキップ（`[HUTABA_FRUSTRATION]` ログ記録）。
  - 同一スレッドセッションにおける返信を1サイクル見送り、自律返信クールダウンとして連携。

### 28. スレッド外固有名詞・架空人名遮断と文除去フォールバック (BAN-168, BAN-171準拠)
- **課題と背景:**
  - 虹裏スレッドにおいて、スレッド内に存在しない架空の人物名や助っ人外国人名（例: 「アダム・リゲイの二番煎じ」等）をLLMが勝手に捏造して語ってしまい、不自然な知ったかぶり・ハルシネーションが発生する課題を根本解決。
- **スレッド全体語彙集合（thread_terms）の構築:**
  - スレッド内の全レス（OP、取得レス、全件保持レス `all_replies`）およびスレッドタイトルから `_extract_hutaba_named_terms` を用いて共有語彙集合を事前抽出。
  - ペルソナ表示名（`BOT_NAME`、`コイト` 等）や自称一人称（私、あたし、僕、俺、自分、うち）、板スラング辞書の見出し語を登録し、誤検知を遮断。
- **スレッド外固有名詞・人名検査 (`find_ungrounded_person_names`):**
  - オウム返しチェック直後、採用直前に決定論的な文字列処理で検査（追加LLM呼び出しなし）。
  - パターンA（フルネーム・中黒名）: `_PERSON_FULLNAME_RE`（`[ァ-ヶー]{2,}・[ァ-ヶー]{2,}`）にマッチし、スレッド語彙や構成パーツ（`parts`）に含まれない中黒連結名を検出。
  - パターンB（人物文脈共起カタカナ語）: `_PERSON_CONTEXT_PATTERN`（の二番煎じ、みたいな、級の、選手、投手、打者、監督、コーチ）と同一文内で共起するスレッド語彙外のカタカナ語を検出。日常・ネット俗語や野球一般用語（`_COMMON_KATAKANA_NON_PERSON_WORDS`）を除外リストで保護。
- **再試行バリアント（`anti_name_hallucination`）と文除去フォールバック (`strip_ungrounded_person_sentences`):**
  - 違反名検出時、初回生成であれば該当名を名指しした再生成指示（`anti_name_hallucination` バリアント）を追加して再試行。
  - すべての候補が棄却された場合でも、`HUTABA_NAME_GROUNDING_STRIP_FALLBACK`（既定 `True`）により捏造名を含む文のみを除去し、残りの正常な文を抽出して安全に投稿文として採用。
- **プロンプト常設指示の強化:**
  - 全バリアント共通のガイダンス（`meta_commentary_guidance`）に【スレッド外の固有名詞・人名の捏造厳禁】を常設追加。スレッドにない名前の捏造・引用を禁止し、内容・描写のみでの反応を強制。

### 29. スレ画特定調査（search_image 連携）の候補取り込み強化とタイムアウト整合
- **課題と背景:**
  - 住民からの「これ誰？」「元ネタ何？」という画像特定質問に対し、従来の画像検索ではタイムアウト（12秒）したり、無関係なファイル名検索の結果が混ざる課題が存在した。
  - ハーネス側の SauceNAO / trace.moe 連携による構造化データ（`candidates`）の提供に合わせ、高精度な特定情報を取り込んでスレ画特定調査メモを劇的に強化。
- **構造化候補データ（`candidates`）の解釈と優先選択ロジック:**
  - `ImageSearchToolAdapter.search_image_unified` の戻り値から `candidates`（`title`, `creator`, `similarity`, `verdict` 等）を解析。
  - ①ビジョン判定 `verdict == "match"` または類似度85%以上（`similarity >= 85.0`）の強い一致候補を最優先で探索。
  - ②該当がなければ先頭候補を参照し、作品・キャラ名『{title}』、作者情報（`（作者: {creator}）`）、一致度（`[一致度: {sim:.0f}%]`）を構造化して `スレ画特定調査メモ` に反映。
- **後方互換性とフォールバック:**
  - `candidates` が存在しない旧ハーネス応答（`text` のみ）やモックでもフォールバックして動作を継続。特定不能時（`success: False` または「特定できませんでした」）は不正確なテキストの誤注入を遮断。
- **タイムアウト設定の最適化:**
  - 並行取得時間を考慮し、`IMAGE_SEARCH_API_TIMEOUT_SEC`（既定 `15.0` 秒）によりタイムアウトを最適化。

### 30. ふたば投稿前「前提グラウンディング・用語崩れ・安心断定」3段チェックおよび文除去フォールバック（507型事故対策） (BAN-13, BAN-18, BAN-105, BAN-159準拠)
- **課題と背景:**
  - 2026-11-11のNIKKEマルウェア騒動スレにおいて、相手が言っていない経歴前提（「4年やってる」）の勝手な借用、スレ頻出語（「クリーンインストール」）の途密切断崩れ（「クリーンイン」）、および安全性未確定話題への無条件の「安心ですよ…！」断定が重なったNo.507事故投稿が発生した。
  - これを防止するため、固有名詞グラウンディング直後に決定論的な3段検査（追加LLM呼び出しなし）を導入。
- **3段チェック機構:**
  1. **前提借用チェック (`find_borrowed_premise`):**
     - 引用元レスに含まれていない相手の経歴・期間前提（「(\d+)年やってる」「長年」「ずっとやってる」等）を検出。スレ全体の他レスに書かれていても引用元レスになければ違反とする。
  2. **用語崩れチェック (`find_collapsed_terms`):**
     - スレッド全域の全レスからカタカナ複合語頻度表（`frequent_terms`、5回以上出現）を構築。定型用語の真の接頭辞でありながらスレ内出現ゼロの崩壊語（「クリーンイン（→ クリーンインストール）」等）を検出。
  3. **安心断定チェック (`find_unconditional_reassurance`):**
     - 安全性の問いかけ（「大丈夫？」「安全？」等）に対し、条件付け表現（「〜なら」「〜を確認して」「〜の場合は」等）を伴わない無条件の安心断定（「安心ですよ」「大丈夫ですよ」等）を検出。
- **再試行バリアントと文除去フォールバック (`strip_premise_grounding_sentences`):**
  - 初回違反時はそれぞれの違反内容に応じた再生成プロンプト（`anti_borrowed_premise`, `anti_collapsed_terms`, `anti_unconditional_reassurance`）を動的追加して再試行。
  - すべて棄却された場合でも、`HUTABA_PREMISE_GROUNDING_STRIP_FALLBACK`（既定 `True`）により違反文のみを除去し、健全な文が残れば安全に投稿文として採用。
- **サルベージオプトイン:**
  - `_call_img2chan_ollama_json` 呼び出しに `salvage_comment_on_truncation=True` を渡し、JSON思考欄（thought）切断時でも良質な初稿コメントを破棄せず救出。
- **プロンプト常設指示の強化:**
  - `meta_commentary_guidance` に【前提借用・用語崩れ・安心断定の厳禁】を常設追加。

### 31. ふたば投稿前「ペルソナ口調喪失グラウンディング」チェック（匿名掲示板常体化防止）
- **課題と背景:**
  - 大相撲スレ等において、直近レスの文体に引きずられてペルソナ語尾や一人称が抜け落ち、掲示板常体（「〜だからな…」「〜だろな…」等）のまま投稿されてしまう現象を防止。
  - プロンプトの思考メモで語尾が計画されていても、出力時に掲示板常体へ同調して口調が崩れるのを決定論的文字列処理で水際棄却する。
- **判定ロジック (`find_persona_tone_loss`):**
  - 追加LLM呼び出しを行わず、以下の2条件を両方満たす場合のみ保守的に違反と判定：
    - **条件A:** ペルソナマーカー（`PERSONA_TONE_MARKERS` 設定値、未指定または空時は既定の `_PERSONA_TONE_MARKERS`: `んですよ`, `ですよ`, `ますよ`, `ですね`, `ますね`, `んです`, `ちゃい`, `ちゃう`, `ぴゃっ` 等の9語）および `BOT_FIRST_PERSON`（一人称）が本文中に1つも存在しない。
    - **条件B:** 掲示板常体の文末（`_PLAIN_TONE_ENDING_RE`: `だからな`, `だろな`, `だよな`, `だなぁ`, `だなあ`, `だな`, `じゃね`, `だぜ` 等）が文末に1つ以上存在する。
  - **実効マーカー正規化と引数インターフェース (`_resolve_persona_tone_markers` / `markers`):**
    - `find_persona_tone_loss` は `markers: Iterable[str] | None = None` 引数を受け取り、呼び出し側から config 設定値（`PERSONA_TONE_MARKERS`）を正規化（文字列のカンマ分割またはコレクション要素抽出）して渡すことで柔軟に語尾マーカーを動的連動。`markers` が `None` または空の場合は既定の9語へ安全にフォールバック。
  - **免除条件:** 本文（非引用部分）が `HUTABA_PERSONA_TONE_EXEMPT_MAX_CHARS`（既定 20文字）以下の極短文・定型句・大喜利ツッコミは対象外として許容。
- **再試行と棄却:**
  - 違反検知時は `persona_tone` バリアントを追加し、ペルソナ語尾を用いた再出力を要求。
  - 口調喪失は文除去で救済できないため部分削除フォールバックは行わず、健全な候補が得られない場合は安全に棄却。

### 32. スレ画特定におけるビジョン検証（Vision Verify）配線と best_match 選択防御
- **課題と背景:**
  - ふたばのスレ画特定（`_resolve_hutaba_thread_topic_context`）において、画像検索アダプタ `search_image_unified` の呼び出し時に `vision_describe` / `vision_verify` を渡しておらず、ビジョン検証が常時オフで類似度のみの仮判定で特定が進む課題を解決。
  - 成功時の `best_match` 選択でタイトル空の候補が誤って採用されるリスクを防止。
- **動的インポートと二重フォールバック:**
  - `front.discord.common.message_media` から `describe_image_for_identification` と `verify_image_candidates` を動的インポート。取得失敗時はログ（`WARN`）を出力し従来動作（引数なし）へ安全にフォールバック。
  - 取得できた場合は `search_image_unified(op_img_url, query=..., vision_describe=..., vision_verify=...)` として呼び出し、`TypeError` 発生時は即座に引数なし呼び出しへ再試行フォールバック（Discord側コマンドと同等の二重防御）。
- **タイトルあり候補の優先選択（best_match 防御）:**
  - `verdict == "match"` または類似度85以上の高スコア候補であっても、`cand.get("title")` が空文字のものは最優先ループで採用せずスキップし、タイトルが明記された候補を優先選択。
  - タイトル空の match 候補しかない場合は先頭候補参照に落ち、タイトルが無ければ特定情報なし（テキストフォールバック）として安全に処理。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `front/hutaba/` 配下のファイルのみを編集すること。**
   - 他プラットフォーム（`front/discord/`, `front/x/`）や `front/` 直下、`harness/agent/*`, `ollama_bot/` のファイルを直接編集してはならない。
2. **【他プラットフォーム直接 import の禁止】**:
   - `front.discord` や `front.x` を直接 import しないこと。
3. **【プロンプト生成ロジック直書きの禁止（原則項目1.17）】**:
   - プロンプト生成・構築ロジックを本サブプロジェクト内に直書き・二重管理しないこと。必ず `harness.agent.prompt_parser.prompt_manager.board_prompts` へ委譲すること。
4. **Mixin継承フォールバックガードの徹底**:
   - Discord Cogに継承されて動作するため、`self` 経由のメソッド呼び出し（`_yield_to_user_replies`、`_call_reply_model_or_fallback`、`_update_emotion_state`、`_sync_emotion_presence` 等）は必ず `hasattr` / `callable` によるフォールバックガードを徹底すること。
5. **Shift_JIS デコード・HTML サニタイズと多重防御**:
   - `*.2chan.net` との HTTP / MCP 通信では Shift_JIS (`cp932`, `errors="replace"`) デコード、タイムアウト捕捉、および投稿文字列サニタイズ（禁止表現・外部招待URL遮断）を徹底すること。
6. **【公開・デプロイスクリプトとの同期義務（項目166）】**:
   - ディレクトリ構造変更や新規共通モジュール追加時は、`customize_for_github.py` の `allowed_items`、`mypy.ini`、`pytest.ini`、`discord-ai-bot.code-workspace` の同期漏れがないか確認すること。

---

## 🛠️ 検証手順
```bash
./front/hutaba/check.sh
# またはルートから:
PYTHONPATH=mock_libs:. pytest --import-mode=importlib front/hutaba/tests tests/test_img2channet.py
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini front/hutaba
```
