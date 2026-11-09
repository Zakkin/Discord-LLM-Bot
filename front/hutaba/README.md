# Front Hutaba (`front/hutaba/`)

`*.2chan.net`（ふたば☆ちゃんねる）を操作・監視・自律投稿するスクリプト群を格納する独立サブプロジェクトです。

## 主な機能
- **自発スレッド巡回・選定 & 気分によるimg/may選定 & 終盤スレ・10分以内消滅スレ除外 & 配信・実況スレ除外 & リクエスト・お題募集スレ除外 & 初回投稿失敗時ゾンビ監視防止:** カタログからペルソナ・興味に基づき健全なスレッドを自動選定し、定期的に自発書き込みを実施 (`_img2chan_proactive_post_loop`)。タイトル・OPが8文字以下かつレス数200件以上の定時・実況スレッド（「むっ！」等）を優先度引き下げで除外し、日常雑談スレを優先選定。感情状態（`mood`, `dominant_emotion`, `curiosity`, `excitement`, `fatigue` 等）と巡回履歴（飽き防止）から `img.2chan.net` / `may.2chan.net` の巡回先を動的決定 (`_select_hutaba_patrol_board`) し、0件時は他方へ自動フォールバック。レス数が650件を超える終盤スレッド（`HUTABA_MAX_PROACTIVE_THREAD_RES_COUNT`、既定 `650` 件）を自発選定から自動除外して書き込み直後のdat落ちを防止。`parse_hutaba_thread_expiration` により `頃消えます` や古いスレ警告を解析し、残り10分（`HUTABA_MIN_THREAD_REMAINING_SEC`、既定 600秒）以内のスレッドは自動除外 (`_is_hutaba_thread_expiring_soon`)。YouTube等の動画・配信URLや生放送・実況・歌枠等のキーワードを含むスレッドも自動除外（`HUTABA_EXCLUDE_STREAM_THREADS`、既定 `True`）。AIリクエストやお絵描き・コラお題募集スレッドも自動除外（`HUTABA_EXCLUDE_REQUEST_THREADS`、既定 `True`、`_is_img2chan_request_thread`）。初回投稿が見送られた・失敗した場合は監視セッションを開始せず即座に終了（`_run_img2chan_thread_post_after_delay`）。初期起動待機（`HUTABA_PROACTIVE_POST_INITIAL_DELAY_SEC`）の既定値も 120.0秒 に延長
- **スレッド完走ケア & 荒らし早期撤退 & 被返信レス限定反応機構（誰の返信もついてないレスへの返信完全防止）:** スレが落ちるまで継続監視しつつ、過激な暴言やスクリプト荒らしを検知した場合は安全に早期撤退 (`Vandalism Detection`)。自発的相槌・追撃時（`_maybe_schedule_img2chan_autonomous_chitchat`）においてスレッド内の被引用・被アンカー数（`calculate_img_thread_reply_counts`）を解析し、他者から既に返信がついているレス（被返信数 > 0）のみに限定してターゲットを選定（返信がついているレスが存在しない場合は相槌タスクを見送り）。フォールバック引用推定時（`_infer_fallback_quote_post_no`）も誰の返信もついてないレスを完全に対象外として除外し、単独書き込み（返信なしタイプ）を維持
- **Discordユーザー返信最優先 (Yield) & 返信キュー・前処理待機完全連携:** Discord返信推論中（`_active_user_reply_count > 0`）のみならず、ターン処理中（`_active_user_reply_turn_count > 0`）や返信キュー待機中（`_reply_queue.qsize() > 0`）、Discord Cog 側のビジー判定（`_is_user_reply_busy()`）を包括的に検知し、完全にアイドルになるまで自発巡回・スレ画Vision推論・レス生成を譲歩・待機（`_is_discord_user_reply_busy`, `_wait_for_discord_user_replies_idle`）。中断後はチェックポイントから安全に途中再開 (`Stateful Resumption`)
- **Discord返信生成パイプライン流用 & 匿名ユーザー配慮 & 中国語混入サニタイズ:** 固定JSONプロンプトを排し、Discordと同じ高品質な返信生成機構を活用。思考タグ除去、相手呼称（特定個人名）排除、Shift_JIS互換サニタイズを徹底。さらにLLM生成文に含まれる簡体字（`劲`→`気`、`试`→`試`等）や中国語語彙リーク（`劲儿`→`っぷり`/`気合い`、`为什么`→`どうして`等）を水際で自然な日本語に自動置換・サニタイズ（`_sanitize_img_post_chinese_leak`）し、英単語（`ComfyUIのLoRA` 等）は維持
- **感情値変動 & Discord概要欄 (Presence/Bio) 連動 & LLM動的感情理由作文:** スレッド書き込み直後に、スレッド話題・としあきの発言・書き込んだレス本文に基づき、LLM（`_generate_hutaba_post_emotion_via_llm`）自身が内的感情（dominant_emotion、scores）と具体的な感情理由（`reason`、句点終わり1文）を動的に作文・反映（未接続・失敗時は `_build_hutaba_post_emotion_reason` へフォールバック）。スコア自然減衰（decay）により直前感情との不整合（BAN-144）を解消し、DiscordのPresence/Bioへ即座に同期
- **自己返信・自己連投完全防止ガード:** スレ末尾が自身の場合や直近の自己投稿後に他人の新着レスがない場合の追撃抑止、自身の投稿（番号・本文一致）への自己返信タスクの破棄、引用返信候補からの自己レス完全除外などによる多重防御
- **長期記憶接続 & 長大スレッド序盤・中盤文脈保持:** `memory.db` から関連記憶およびチャンネル要約を動的取得してプロンプトへ注入（全体フォールバック検索・エコー汚染サニタイズ完備）。スレッド長大化時（>8レス）にOPや序盤〜中盤の重要レス（引用・疑問・宣言・そうだね等）を最大6行抽出し、文脈の一貫性を保持 (`_extract_early_thread_context_lines`)
- **初回投稿時の引用先解決 & オウム返し・自己投影ミラーリング検査・リトライ:** 初回投稿時（`target_reply is None`）でもLLM応答のJSONパースや語彙重複（`_infer_fallback_quote_post_no`）から引用先レス（`effective_target_reply`）を逆引き解決。相手の行動語だけを拾って「私も毎回〜派なんですよ」「自分も〜するタイプです」と同調する自己投影ミラーリングを棄却し、相手の状況へのツッコミを促す `anti_parrot` プロンプトで動的再試行 (`_generate_hutaba_reply`)
- **特定ペルソナ依存の排除と判定・正規化の汎用化:** `img2chan_posting.py` 内に存在した特定キャラ名（「コイト」等）や特定感嘆詞・語尾（「ぴゃっ」等）を排除。普遍的な日本語一人称（`私`, `わたし`, `あたし`, `僕`, `ぼく`, `俺`, `おれ`, `自分`, `うち`）による正規化・自己投影共感検知（`_SELF_PROJECTION_SYMPATHY_RE` / `_SIBLING_PERSONA_SUFFIX_RE` / `_normalize_for_sibling_plagiarism_check`）へと汎用化し、プロンプト例文や調査注記も普遍的一人称へ統一
- **事前調査メモのノイズ棄却注記 & メタ検索失敗レポート検知・リトライ:** スレッド主題事前Web調査メモに検索ノイズ棄却・報告禁止の注記を付与。「調べてみたけど定義が確認できなかった」「特定の作品のネタ？」等の辞書引き失敗報告を `_HUTABA_META_SEARCH_REPORT_RE` で検知・棄却し、スレの流れに即した自然なレスへ再生成 (`_img2chan_comment_parrot_reason`)
- **中盤レス全件保持 & マクロ話題抽出サマリー & Thoughtスキーマ & 被返信レス事前確定 & 引用剥奪完全廃止と最多被返信レス強制合流:** `parse_img_thread_html` で全件レスを `all_replies` に保持して中盤ドロップを防止。スレッド全域の頻出語比較から初期主題と現在の盛り上がりを2層抽出する `_extract_img2chan_macro_topic_summary` を導入。`IMG_THREAD_POST_COMMENT_SCHEMA` への `thought` 追加（本文漏洩防止ガード付き）。`calculate_img_thread_reply_counts` で `post_no` / `No.post_no` / `number` の全形式キー登録を行い表記揺れによる誤剥奪を防止。さらに進行中スレッド（10レス以上）ではLLM呼び出し前に最多被返信レスを `target_reply` として事前確定させ、LLMが孤立レスや空配列を返した場合でも引用剥奪（空配列化）を完全廃止して最多被返信レスへ強制合流させる7重防御を配備。未返信レス指定時も会話ツリーの親レス（被返信あり）へ自動補正し、100%確実に誰かの返信がついているレスへの引用返信を成立させる（10レス未満の初期スレのみ単独投稿を許容）
- **対象レス周辺文脈（直系子レス・直前会話・過去ローカルミーム）の厳密抽出とプロンプト注入:** 最多被返信レスが直近10件より前に位置する場合の話題孤立・キメラ合成（BAN-169）を防止するため、1重引用（`>`）のみで判定する直系子レス（孫レス `>>` の誤抽出防止）、直前の先行会話スライス、および過去ログからのキーワード言及レス（ローカルミーム）を厳密に抽出してプロンプトへ注入（`_extract_hutaba_target_context_lines`）。`calculate_img_thread_reply_counts` も `max_targets=1` に制限し孫引用による被返信数水増しを排除。`build_img_thread_followup_post_prompt` への `target_context_lines` 連携と階層的 `TypeError` フォールバックを備え安全に稼働
- **修辞的疑問の検索誤爆防止 & Thoughtスキーマ拡張 & スレ内風刺・反復ミーム抽出:** 「〇〇はどう思う？」「〇〇はどうすんだろ」等の第三者・著名人への修辞的疑問を `_HUTABA_RHETORICAL_OPINION_RE` で検知して解決策Web検索から除外（「これどう思う？」等の通常相談は維持）。`IMG_THREAD_POST_COMMENT_SCHEMA` の `thought` を意図分類・大喜利戦略（尊大・無責任な代弁または冷たい定型句突き放し）のステップへ拡張しマジレスを防止。さらに `_extract_img2chan_macro_topic_summary` において固有名詞・カタカナ語（いじり対象）や複数レスで出現・引用される短文フレーズ（定型ツッコミ・ミーム）を汎用解析してマクロ要約注記へ付与
- **先行会話スライス抽出 & Thoughtスキーマ改修（クリティカル思考強制） & 文脈保護 & Hook Echo防御 & 略称同義語展開・無関係レス混入防止:** 対象レスの直前1〜3レスを先行会話スライスとして遡り取得し、議論の流れ（幻滅・ツッコミ・泥沼の実態）を保持（`_extract_hutaba_target_context_lines`）。引用なしレスで直前に挟まった無関係な別話題レス（時岡リメイク等）を自動除外しつつ、AIモデル・技術用語等の略称・表記揺れ同義語（`NAI` ⇔ `novelai`, `LoRA` ⇔ `Lola`, `ComfyUI` ⇔ `Comfy`, `SD` ⇔ `StableDiffusion`, `PixAI` ⇔ `ピクスAI` 等）を `_extract_hutaba_named_terms` で自動展開してスレッド内先行言及レス（`[スレッド内関連レス]`）を確実に抽出・注入。ハーネス互換フォールバック時も文脈を破棄せずチャンネル要約へ安全にマージ。`IMG_THREAD_POST_COMMENT_SCHEMA` の `thought` を Step A（先行文脈特定）・Step B（主張検証とクリティカル・カウンター検討）・Step C（表現サニタイズ）の3段階推論へ改修し安易な全肯定ポエムを遮断。文頭フレーズ反復＋抽象ポエム（「〇〇…！あの頃の熱さ〜」等）を `hook_echo_abstract_poem` として水際検知・棄却（`_img2chan_comment_parrot_reason`）
- **スラング辞書連携による検索クエリ除外 & 用語注意メモ注入 & 周辺レス内在化 & 既存レスパクり・キメラ合体検知（共通話題語誤検知防止）:** 「すみフラ」「うろフラ」「フラ」等の質問・相談前置きスラングを検索トークンから自動除外（`filter_query_terms_by_slang`）し、ノイズ検索を遮断。対象レスからスラングを検出してプロンプトへ【スレッド用語・スラング注意メモ】を注入（`lookup_slang_in_text`）。周囲のツッコミを他人事報告するメタ実況やキメラ疑問文をプロンプト指示および `_img2chan_comment_parrot_reason`（`meta_commentary_report`）で抑止。さらにスレッド内の他人の既存レス（子レスや周辺レス）の語尾変えパクりや複数レスを繋ぎ合わせたキメラ合体を `sibling_reply_plagiarism` として水際検知・棄却（文字クラス正規化バグの解消、対象レス・OPの共通話題語2-gram除外による誤検知防止、8文字未満極短レス除外、非アンカー本文引用行の先行サニタイズ除去を完備）し、既出ツッコミを除外した独自視点でのレス生成を強制。事前ターゲット選定でも直近20件以内の被返信レスを優先し古いレスへの誤固定を防止
- **スレ画（OP画像）URL抽出 & Qwen3-VL-8Bキャプション化 & タイムアウト拡張・KVキャッシュ最適化 & VRAM即時解放 & LRUキャッシュ:** `parse_img_thread_html` によるスレ画フルURL・サムネイルURLの正規表現抽出。`caption_hutaba_thread_image` によるサムネイルダウンロードと `Qwen3-VL-8B`（`OLLAMA_VISION_MODEL`）による客観的描写・文字起こしキャプションの非同期生成。Cold Start時のモデルロードおよび画像推論時間に耐えるためタイムアウトを60秒へ拡張（`OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC`、既定 `60.0` 秒）、およびコンテキストサイズを4096に最適化（`OLLAMA_VISION_NUM_CTX`、既定 `4096`）。推論後は `release_ollama_model` でVRAMを0MBに即時解放（BAN-170, BAN-180準拠）。さらに同一画像URLへのインメモリLRUキャッシュ（最大128件）および `fetch_img_thread` への `cached_caption` 引数連携により、スレッド監視ループや投稿反映確認時の短時間重複推論（BAN-94）を透過的に防止。スレッド調査直接返信（`build_img_thread_direct_reply`）、スレッド要約（`format_img_thread_summary`）、レス生成（`_generate_hutaba_reply`）のOPコンテキストにスレ画内容を注入し、本文なしスレや画像ネタスレでの文脈理解精度を向上
- **スレ画の自律画像検索・特定機能 (search_image 連携):** 「これ誰？」「何のキャラ？」「元ネタ何？」といった画像特定質問レスを `_HUTABA_IMAGE_QUESTION_RE` で自動検知し、`ImageSearchToolAdapter.search_image_unified` を介してスレ画（`op_image_url`）を逆画像検索。特定されたキャラクター・人物情報を `スレ画特定調査メモ` としてプロンプトに注入し、画像特定質問に対して自然かつ的確な回答を提供。スレッド要約（`build_img_thread_direct_reply`）への特定結果（`op_image_search_summary`）反映や未配備時のWeb検索フォールバックも完備
- **統一ログ接頭辞 & 投稿内容ログ記録:** すべてのログに `[hutaba] ` を付与し、ロガー名を `front.hutaba` に統一。レス文生成完了・投稿開始・成功・失敗・スレッド反映確認の各フェーズで発言内容（`comment`）を確実に記録
- **may.2chan.net サポート & 画像投稿 (HTTPマルチパート `upfile` 添付):** `img.2chan.net` に加え `may.2chan.net` スレッドの正規化・HTML解析・キャッシュトークン取得・レス投稿に完全対応。`submit_img_thread_reply` に `image_path` / `image_bytes` / `image_filename` 引数をサポートし、画像指定時は Chrome MCP から直接 HTTP 投稿（`aiohttp.FormData` マルチパート `upfile` 送信）へ安全にフォールバック
- **ユーザー指定スレッド重点巡回 (Focus Patrol) & 複数URL先頭限定 & 自発巡回プリエンプション:** Discordで貼られた複数スレッドURLから先頭1件のみを確実に監視対象とし、既存の自発巡回スレッドを即時中断（Preemption）してユーザー指定スレッドへ最優先切り替え。初回遅延（`IMG2CHAN_USER_REQUESTED_POST_DELAY_SEC`、既定 5.0秒）、ポーリング間隔（`IMG2CHAN_USER_REQUESTED_POLL_SEC`、既定 15.0秒）、相槌クールダウン（`IMG2CHAN_USER_REQUESTED_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC`、既定 120.0秒）を短縮し、ユーザーが貼ったスレッドの監視・対話を最優先
- **あぷ小動画レス除外 & 静止画自動キャプション認識 & 引用先不変性ガード (BAN-169, BAN-170):** レス本文中の動画ファイル（`fu\d+\.mp4`, `webm` 等）を `_has_hutaba_unsupported_media` で自動検知し、自発相槌・事前返信先選定・追撃候補から除外してハルシネーション返信を防止。あぷ小・あぷの静止画像（`fu\d+\.(jpg|png|webp)`）はVisionモデルで自動キャプション化してプロンプトへ注入。さらに `target_reply` 指定時は被返信数0件の新着レスであっても親レスへのすり替え補正を遮断し引用先を100%固定。`compose_img_thread_reply_comment` で前レス引用（`>...`）を含む相手に対して自身の非引用行を優先引用し二重引用を完全防止
- **思考先行 (Thought minLength: 10) & 検索クエリ作品ドメイン優先 & 直前文脈保護 (BAN-169, BAN-171):** `IMG_THREAD_POST_COMMENT_SCHEMA` の `thought` に `minLength: 10`、`comment` に `minLength: 1` を追加して空思考でのバイパス・トートロジー返信を遮断。`_generate_hutaba_reply` で `IMG2CHAN_POST_THINK` / `OLLAMA_MAIN_MODEL_THINK` 設定を反映して推論モデルの思考オプションを尊重。`_HUTABA_TOPIC_STOPWORDS` に一般語（「無理」「面白」「所他」等）やR18ノイズ語（「デカパイ」等）を追加拡充してノイズ検索を遮断。スレッド主題・作品ドメイン優先の検索クエリ生成（マクロ主題 `macro_topic_summary`、OPタイトル/テキスト、スレ画キャプション由来の語を最優先配置）により高精度なWeb調査を実現。直前会話スライス抽出（`_extract_hutaba_target_context_lines`）において直前1件（`target_idx - 1`）を会話の直系先行文脈として無関係別話題判定から除外して必ず保持

## 主な設定環境変数
| 環境変数名 | 型 | デフォルト値 | 説明 |
| :--- | :--- | :--- | :--- |
| `HUTABA_PROACTIVE_POST_ENABLED` | bool | `True` | カタログからの自発スレッド選定・投稿ループの有効化 |
| `IMG2CHAN_POST_THINK` | bool | `False` | ふたばレス生成時の推論モデル思考（think）有効化フラグ（未指定時は `OLLAMA_MAIN_MODEL_THINK` にフォールバック） |
| `HUTABA_PROACTIVE_POST_INITIAL_DELAY_SEC` | float | `120.0` | 起動直後のDiscord返信優先のための自発巡回開始初期遅延秒数 |
| `HUTABA_MAX_PROACTIVE_THREAD_RES_COUNT` | int | `650` | 自発新規選定から除外する最大レス数閾値（終盤スレッド除外） |
| `HUTABA_EXCLUDE_STREAM_THREADS` | bool | `True` | 配信・生放送・実況・耐久等のスレッドを自発選定から自動除外 |
| `HUTABA_EXCLUDE_REQUEST_THREADS` | bool | `True` | AIリクエスト・お題募集等のスレッドを自発選定から自動除外 |
| `HUTABA_EXCLUDE_EXPIRING_THREADS` | bool | `True` | 10分以内に消滅するスレッドの自発選定除外フラグ |
| `HUTABA_MIN_THREAD_REMAINING_SEC` | float | `600.0` | 自発選定から除外する残り消滅時間閾値秒数（既定10分） |
| `HUTABA_PATROL_BOARDS` | str | `"img,may"` | 自発巡回対象サーバー（`"img"`, `"may"`, `"img,may"`） |
| `HUTABA_MAY_PATROL_BASE_PROB` | float | `0.50` | 気分判定前の `may.2chan.net` 巡回基準確率 |
| `HUTABA_AUTONOMOUS_CHITCHAT_ENABLED` | bool | `False` | 引用返信なし時の自発的相槌・追撃レスの有効化（慎重運用） |
| `IMG2CHAN_USER_REQUESTED_POST_DELAY_SEC` | float | `5.0` | ユーザー指定スレッド検知時の初回レス投稿遅延秒数（短縮） |
| `IMG2CHAN_USER_REQUESTED_POLL_SEC` | float | `15.0` | ユーザー指定スレッドの重点巡回監視ポーリング間隔秒数 |
| `IMG2CHAN_USER_REQUESTED_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC` | float | `120.0` | ユーザー指定スレッドの自発的相槌クールダウン秒数 |
| `IMG2CHAN_AUTO_POST_DEDUP_TTL_SEC` | float | `3600.0` | 直近投稿済みスレッドの重複防止保持期間秒数 |
| `IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE` | bool | `True` | アクティブスレッドの排他制御（1スレッド集中巡回） |
| `OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC` | float | `60.0` | スレ画Visionキャプション生成のタイムアウト秒数（Cold Start対応） |
| `OLLAMA_VISION_NUM_CTX` | int | `4096` | スレ画Visionキャプション生成時のコンテキストサイズ |

## ディレクトリ構成
- `client/`: スレッド取得・HTML解析・投稿文サニタイズ・送信・カタログ取得 (`img2chan.py`, `img2channet_parts/`。プロンプト構築正本は `harness` へ委譲)
- `monitor/`: スレッド監視・自発巡回投稿・そうだね学習・感情連携 (`img2chan*.py`)
- `thread_reader.py`: スレッド調査結果からの返信文整形
- `tools.py`: `img_top5_dispatch` / `img_thread_dispatch` / ヘルパーローダー
- `utils.py`: スレッドURL正規化・HTTPヘッダー生成
- `adapter.py`: `HutabaAdapter`（`PlatformAdapter` 実装）
