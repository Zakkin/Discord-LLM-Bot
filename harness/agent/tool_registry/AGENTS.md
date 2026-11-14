# 🤖 Tool Registry サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/tool_registry/`）は、AIエージェントの**フェーズ3（Action/ツール選択と実行）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **ツールレジストリと実行基盤**:
   - `ToolRegistry`, `ToolExecutor`, `ToolDefinition`, `ToolParameter` によるスキーマ管理と実行制御。
   - `ToolCategory`（`WEB`, `MCP`, `WEATHER`, `SYSTEM`, `CUSTOM`）によるツールの明確な分類とライフサイクル管理。
2. **MCPおよび外部ツール連携**:
   - Chrome Browser MCP, 天気（Weather）API（ふたば `*.2chan.net` 操作本体は `front/hutaba/`、X `x.com` 操作本体は `front/x/` に分離し、`front.registry` 経由で連携）。
   - 外部Web調査（Search / Extract）および事実検証（Fact Check）。
   - バッチリサーチアダプター（`adapters/web_run.py` によるコマンド束ね型検索・開く・行番号付きページ表示・ページ内検索＋`web_research/page_registry.py` によるメモリ内ページ保持）。
   - 逆画像検索アダプター（`adapters/image_search.py` による専用逆検索API（SauceNAO / trace.moe）並行呼び出し＋自前ビジョン検証・正直な結果生成・コンテキスト画像URL自動抽出）。
3. **ゲーム・特殊アクションアダプター**:
   - 特殊アクションアダプター（`adapters/special_action.py` による要約・解説・これ何・ファクトチェック処理）。
   - ゲームツール基盤（20doors / うみがめ、お題生成、`GamePoolManager`）。
4. **歌唱・音楽アシスタント基盤 (`singing_helpers.py`)**:
   - 歌唱リクエスト判定、楽曲名抽出、テンポ（BPM）解析、リズム・フレーズ待機時間動的計算。
   - 歌詞スクレイピング（歌ネット、うたてん、Wiki、ブログ等）および歌詞サニタイズ（思考タグ・ダミー行・目次メタキーワード・HTML残骸記号の除去）。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/tool_registry/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `observation/`, `memory_manager/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **外部通信の包括的例外処理と多重防御**:
   - 外部APIやMCPサーバーとの通信において、接続タイムアウト、切断（ServerDisconnectedError）、不正JSONなどの例外を確実に捕捉し、上位プロセスをクラッシュさせてはならない。
3. **目的語ハイジャック遮断とクエリ検証**:
   - Web調査や特殊アクションにおいて、直前の無関係な雑談から勝手に単語を取り込んで目的外の検索クエリを生成（目的語ハイジャック）してはならない。
4. **ツール実行結果の型安全性**:
   - ツール実行結果（`ToolResult`）は、フェーズ4（Observation）が安全に解釈できるよう、一貫したフォーマット（`output`, `error`, `success`）を維持すること。
5. **Webスクレイピングにおける目次（TOC）誤検知防止とサニタイズ徹底（多重防御）**:
   - Wikiやブログ等のHTMLスクレイピングにおいて、単語境界（`\b`）等による曖昧な見出し探索を行ってはならない（目次アンカー `<a href="#歌詞">` や `<span class="toctext">` に誤マッチし目次項目を本文と誤認する原因となる）。
   - HTMLパース前に目次コンテナ（`id` または `class` に `toc` や `table-of-contents` を含む `div`, `nav`, `section`, `ul`, `ol`）をネスト構造を考慮して丸ごと除去し、正規の見出しタグ `<h([1-6])>` のみを走査して同等以上の見出しまでの歌詞セクションを安全に切り出すこと。
   - ダミー行判定（`is_dummy_lyrics_line`）およびサニタイズ（`sanitize_lyrics_lines`）において、先頭残骸記号（`">`, `>`, `"`, `'` 等）のトリミングと目次メタキーワード（概要、目次、歌詞、動画、関連項目等）の除外による多重防御を徹底すること。
6. **日常カタカナ語・文字パズル・なぞなぞ・ヒント文・一般疑問詞/副詞表現・PC日常語による未知語Webリサーチ誤発火の防止（多重防御）**:
   - 会話やクイズで日常的に使われる一般カタカナ語（「ヒント」「クイズ」「パズル」「ルール」「スタート」等）および一般語・一般疑問詞・副詞表現（「画数」「文字」「数字」「記号」「暗号」「法則」「一体」「一体何」「一体全体」「何事」「何故」「何時」等）は、必ずストップワード（`_COMMON_KATAKANA_STOPWORDS` / `_GENERIC_TERM_STOPWORDS`）に登録し、未知語・スラング候補（`candidate_term`）への誤判定を遮断すること。
   - 日常的なPC・ITハードウェア・デバイス用語（「パソコン」「ノートパソコン」「マック」「スマホ」「タブレット」「モニター」「ディスプレイ」「キーボード」「マウス」「ハードディスク」「ストレージ」「メモリ」「プロセッサ」「ルーター」「プリンター」等）および時事・一般語（「最新」「最近」「現在」等）は、必ずストップワード（`_COMMON_KATAKANA_STOPWORDS` / `_GENERIC_TERM_STOPWORDS`）に登録し、「パソコン困りごと」「最新のスマホ」等の日常会話が未知語・スラング候補（`candidate_term` / `slang_or_unknown_trigger`）としてWeb調査を誤発火させる事態を未然に遮断すること（項目128, 145）。
   - 文字パズル、なぞなぞ、暗号の出題やヒント提示発言（例: 「ヒント、一つ画数が増える」「法則は〜」「一 → 二、日 → 白、木 → ○」）は、`looks_like_puzzle_or_riddle` により検出し、URLや明示的な検索指示（「調べて」「検索して」）がない限り、Webリサーチ要否判定（`slang_or_unknown_trigger` / `needs_research` / `should_consult_model`）を一括して抑制すること。
7. **ファクトチェック・事実検証におけるブラウザ調査（browser_search）優先化、検索ノイズドメイン除外、およびクエリリファイン（多重防御）**:
   - `detect_reply_research_rules` / `resolve_research_mode` において、`mode="fact_check"` や真偽検証の文脈（「本当？」「デマ？」「公式設定」「エピローグ」「裏取り」等）を検出した場合、Chrome MCP利用可能環境（`WEB_RESEARCH_BROWSER_ENABLED=True`）ではデフォルトの `simple_search` ではなく `browser_search` を自動解決・返却すること。
   - `research_dispatch` 内で、事実確認モード時は検索結果の上位候補ページ（1〜2件）を実際に `_read_url_with_browser` で開き、本文（作品解説・Wiki・エピローグ記述等）を読み込んで `summary` に含めること。
   - 一般掲示板・知恵袋・成人向け漫画/アップローダー系サイト等のノイズドメイン（`girlschannel.net`, `detail.chiebukuro.yahoo.co.jp`, `z-z.jp` 等）は、事実確認用ソースとしての品質スコア（`_browser_source_quality`）を大幅に減点し、`_prioritize_candidate_sources` により高信頼百科事典（Wikipedia, ピクシブ百科事典等）や公式サイト、大手ニュースサイトを優先してブラウザで開くこと。
   - 初回の複合キーワードで検索結果が得られない場合、クエリ内の固有名詞（例: `チョカグ`）単体や `チョカグ とは` で再検索するフォールバック（2nd pass）を適用すること。
8. **Claim Decomposition（要素分解Fan-out）、スラング保持、サブカルWiki高評価、および要約・抜粋保全（多重防御）**:
   - `_BROWSER_QUERY_SYSTEM_PROMPT` および `_generate_browser_search_plan` において、複合質問に対して「①基本設定・時期・前提」「②理由・経緯・因果関係」「③特有の俗称・スラング・評判」の観点に分解した2〜3個のクエリ展開（Query Fan-out）を行うこと。
   - ネットミーム、空耳、俗称、カギ括弧等の特有表記（例: 「かじゅき」等）は言い換えたり削除したりせず、必ず少なくとも1つのクエリに原文のまま保持すること（`extract_quoted_terms` による多重防御）。
   - サブカルチャーや作品・ネットミーム解説の検索精度向上のため、`w.atwiki.jp` を高信頼ドメイン（`_TRUSTED_FACT_DOMAINS`）に登録し、RRFランキングおよび品質評価で適切に加点すること。
   - 要約トークン枠（`WEB_RESEARCH_SUMMARY_NUM_PREDICT`）のデフォルト値を `768` へ拡大し、要約プロンプト（`_summarize_browser_observations`）を構造化事実メモ（時期・前提、理由・因果関係、俗称・スラング真偽）を保持する指示に強化すること。
   - ファクトチェック時（`is_fact_check=True`）および複数クエリ展開（Fan-out）調査時において、上位ソースの重要生抜粋（`content_excerpt`）を文字数予算の範囲内で要約テキストの後ろに補足として確実に結合・保全すること。
   - 伝聞調（「〜とのことらしい」「〜との事らしいけど」「〜らしいけど」等）の表現を `looks_like_fact_check_or_verification` により幅広く検知し、外部インターフェース（`research_dispatch`, `resolve_research_mode`, `detect_reply_research_rules`, `WebSearchToolAdapter.execute_search`）で明示的な `is_fact_check: bool | None = None` を受け取れるよう後方互換性を維持したまま拡張すること。
9. **Web検索基盤の統合（Chrome MCP優先化・DDG Lite POSTフォールバック）および歌唱曲名保護・Web装飾ノイズ除去・歌詞抽出強化（多重防御）**:
   - 外部モジュール向け統合検索 `search_web_entries_unified` を配備し、Chrome MCP（`_search_web_entries_with_mcp`）最優先試行 -> HTTP直接検索フォールバックの多重防御チェーンを構成すること。
   - HTTP検索フォールバック（`_search_web_entries_http`, `_fallback_http_search`）において、Botブロック（CAPTCHA）を回避するため DuckDuckGo Lite (`https://lite.duckduckgo.com/lite/`) へのPOSTリクエスト（`{"q": query, "kl": "jp-jp"}`）を最優先試行し、取得不可時のみ既存の GET `html.duckduckgo.com` や Google を試行すること。
   - 歌唱機能（`singing_helpers.py`）において、曲名抽出（`extract_song_title`）時に引用符で囲まれている場合（`title_quoted`）は末尾の `(?:の歌|の曲)$` 削除を一切行わず完全な曲名として保護すること。非引用符の場合でも、抽出タイトルが7文字以下（「ドラえもんのうた」等）または「〇〇の歌」自体が曲名本体である場合（「魔を祓う尊師の歌」等）は末尾誤切断を防ぎ保護すること。
   - 曲名一致判定（`is_matching_song_title`）において、Webタイトル装飾ノイズ（`【歌詞】`, `[歌詞]`, `歌詞付き`, `歌詞まとめ`, `公式PV`, `MV`, `FULL`, `off vocal` 等）を事前除去してから正規化・ひらがな照合を行い、サブストリング包含関係の多重チェックを配備すること。
   - 汎用HTMLからの歌詞抽出（`extract_lyrics_from_generic_html`）において、目次（TOC）コンテナブロック事前除去を徹底した上で、歌詞専用コンテナ（`div.lyrics`, `div.kashi` 等）の優先判定および全角空白インデント行や短行の歌詞本文判定を柔軟化すること（項目142遵守）。
10. **URL入力・投稿時の直接閲覧（`browser_read_url`）ルーティング保証およびDNS検証（SSRF対策）の堅牢化（多重防御）**:
   - `WebSearchToolAdapter` およびリサーチディスパッチにおいて、クエリ内にURLが含まれる場合は検索エンジン検索（`browser_search`）に流さず、必ずURL直接読解（`browser_read_url`）へルーティングすること。URL付随コメントがある場合でも安易に `browser_search` へ降格させてはならない。
   - SSRF検証（`_is_safe_public_browser_url_resolved`）において、一時的なDNS名前解決エラー（`gaierror`）によってパブリックURLを危険URLと誤認・遮断してはならず、ポート指定名前解決（HTTP: 80 / HTTPS: 443）、IPv4フォールバック（`AF_INET`）、およびブラウザ側解決への委譲フォールバックによる多重防御を死守すること。
11. **対話相手名（User-First Resolution）の誤Web検索防止ガードおよび検索実行・フォールバック時の構造化観測ログ（多重防御）**:
   - `WebSearchToolAdapter.execute` / `execute_search` において、`context` または引数から対話相手名（`target_name`, `other_names`）を取得し、クエリが相手の名前を含み、かつ雑談の相槌語・親密度語句（「好きだよね」「脈アリ」「仲良し」「ランキング」等）と組み合わされた誤爆検索である場合は、外部検索を実行せずに「『{target_name}』は現在の対話相手（Discordユーザー）の名前であり、外部Web検索の対象ではありません。目の前の相手との会話文脈に基づいて応答してください。」という案内テキストを即座に返し、誤検索および会話文脈の破壊を根本遮断すること（`principles.md` 1.14、`ban_list.md` #92, #114遵守）。
   - URLを含むクエリはURL直接読解として保護し、相手名ガードを誤発火させないこと。
   - 検索実行時およびフォールバック発生時において、`[web-search] Executed query=%r mode=%s fallback_used=%s result_chars=%d` の構造化ログを明示的に出力し、観測性・テレメトリを保証すること。
   - `WebSearchToolAdapter.execute` を `execute_search` のエイリアスとして配備し、後方互換性とインターフェース一貫性を維持すること。
12. **Web調査クエリ生成での話者名・例示語汚染防止、ブラウザ観察要約への現在日時注入と `num_predict` 下限保証、およびPDF直リンク巡回抑止（項目163, 174）**:
   - `_BROWSER_QUERY_SYSTEM_PROMPT` および `_generate_browser_search_plan` において、具体的なスラング例示語（例: `「かじゅき」`）をプロンプト内に記載してモデルに丸写しさせたり、`補助文脈` に含まれる Discord 会話の話者名（`雑菌`, `Assistant` 等）を検索クエリに混入させないこと。生成後のクエリに対しても、`user_text` に含まれない `context` 由来の話者名やプロンプト例示語を機械的に除去するサニタイズを行うこと。
   - `_strip_context_speaker_and_meta_labels` により、会話履歴行からの `[HH:MM] 話者名 [>>返信先]:` プレフィックス、`Assistant` ラベル、および川柳BOT等の `詠み手:` メタ行を事前に除去し、共起語や検索クエリへの話者名・ロールラベル混入を根本遮断すること（BAN-163, BAN-174）。
   - `_build_browser_observation_prompt` および `_BROWSER_SUMMARY_SYSTEM_PROMPT` に現在のサーバ日時コンテキスト（`現在のサーバ時刻は 2026年...`）を注入し、モデルが 2026 年を架空の未来日付と誤認して長大な `insufficiency_reason` を生成し `summary` 途中で JSON 切断（`OllamaJSONTruncatedError`）を起こす事態を防止すること。
   - `_BROWSER_SUMMARY_SCHEMA` / プロンプトで `insufficiency_reason` を 60 文字以内（`insufficient=false` 時は空文字 `""`）に制限し、`_summarize_browser_observations` における `num_predict` の下限を最低 `512`（デフォルト `768`）に保証すること。
   - ブラウザ巡回候補 URL の選定（`_prioritize_candidate_sources` / `_browser_source_quality`）において、Chrome MCP で本文テキスト抽出が困難な PDF 直リンク（`.pdf` や `/_pdf`）を減点・除外し、空テキストによる無駄な 3 回リトライを防止すること。
13. **MCPブラウザ遷移ログにおけるターゲットURL出力仕様およびWeb本文抜粋のUIノイズクレンジング（BAN-139）**:
   - `MCPChromeHTTPClient.call_tool` において、引数に `url` または `query` が含まれる場合は `MCP call_tool started: name=%s target_url=%s`（および完了時 `MCP call_tool finished: name=%s target_url=%s`）として安全にログ出力し、MCPブラウザナビゲーションや検索時の遷移先・追跡性を担保すること。
   - Webページ取得時（特にX等の動的サイト）において、`_cleanup_browser_text` / `_extract_browser_page` による本文抜粋処理時に、ページ内のUIノイズ（インプレッション数・エンゲージメント表示、UIアクションボタン文言、アクセシビリティツリー残骸等）を汎用的にストリップし、後続LLMでのハルシネーションを確実に遮断すること（BAN-139遵守）。
14. **数値＋単位の複合名詞保護、シミュレーション・人体影響検証疑問文のWeb調査検知、および調査思考ポリシー補強（項目92, 102）**:
   - `extract_named_term_candidates` において、「10万ボルト」「50mA」「100km/h」「5000兆円」のように数値や漢字桁・接頭辞が結合した名詞句を単一単位（「ボルト」等）に分解せず、複合語として最優先で抽出すること。単一の一般的な計量単位（ボルト、アンペア、ワット等）はストップワードに登録し、未知語候補への誤爆を防止すること（項目92）。
   - 「〜食らったらどうなる」「〜したらどうなる」「〜の影響」「致死量」「人体への影響」などのシミュレーション・客観的影響検証疑問文を `looks_like_simulation_or_impact_verification` により検出し、`fact_check_flag`, `research_request`, `needs_research` を正しくトリガーして `browser_search` で客観的知見を調査すること。また、トピック評価（`topic_opinion_inquiry`）による評判クエリへの横取り・誤検知を遮断すること（項目102）。
   - ツールカタログ（`ToolCatalogFormatter`）およびWeb検索ツール定義において、【調査思考ポリシー】として未知語・スラングだけでなく「数値や設定に基づく影響・被害・結果のシミュレーション・検証（例: 〜したらどうなる？ 人体への影響は？ 等）」についても推測で済ませず `search_web` を呼び出して客観的知見を調査するようガイドラインを明記すること。
15. **複合動詞によるファクトチェック誤爆防止、Bot名・人称代名詞・日常語のストップワード保護、およびルールマッチング観測ログ（項目128, 145）**:
   - `_FACT_CHECK_QUESTION_RE` において、「合ってる」の前が「付き」「話し」「居合」「持ち」「巡り」「重なり」「噛み」「食い」「助け」「譲り」「見合」「似合」等の複合動詞である場合は負の戻り読み（`(?<!付き)(?<!つき)...` 等）により除外し、「付き合ってるのかい？」「話し合ってるのか？」等の日常対話が `fact_check` を誤発火させる事態を確実に防止すること。
   - `_GENERIC_TERM_STOPWORDS` にBot一般呼称（「アシスタント」「AI」「ボット」「bot」）、人称代名詞・呼称（「貴様」「お前」「手前」「貴方」「自分」「相手」「味方」「敵」）、一般名詞・日常語（「恋人」「彼氏」「彼女」「今恋人」「友達」「今」「記憶」「出来」）を網羅的に登録し、未知語・スラング候補（`candidate_term` / `slang_or_unknown_trigger`）への誤爆を遮断すること。自身の名前・愛称・一人称（`BOT_ALIASES` / `BOT_FIRST_PERSON` / `OLLAMA_BOT_DISPLAY_NAME`）は `get_generic_term_stopwords()` により動的に合流させ、固定ハードコードによる他キャラ運用時の誤動作を防止すること（項目128, 145, 174）。
   - `_PREFERENCE_OR_OPINION_MARKERS` に日常の交際・人間関係表現（「付き合っ」「付きあい」「付き合い」「恋人」「彼氏」「彼女」「仲良し」「仲が」）を追加し、対人関係雑談が会話文脈（`conversational_followup`）として正しく保護されること。
   - `detect_reply_research_rules` において、`log.info("[research-rules] Rule matching details: reasons=%r, candidate_term=%r, named_terms=%r, matched_fc_pattern=%r", ...)` の構造化観測ログを出力し、判定理由と一致パターンの追跡性を保証すること。
16. **日常行動・近況独白文の未知語トリガー抑止ガードおよび強調・誇張カタカナ接頭辞ストップワード追加（多重防御、項目128, 145）**:
   - `web_research/config.py` 内の `_COMMON_KATAKANA_STOPWORDS` に日常の誇張・ゲーム的表現・強調接頭辞（「ノーマル」「レア」「スーパーレア」「ウルトラ」「ウルトラレア」「ハイパー」「メガ」「マスター」「アルティメット」「エクストラ」「プレミアム」「シークレット」）を追加し、未知語・スラング候補（`slang_or_unknown_candidate`）への過剰検知を未然に遮断すること。
   - `looks_like_daily_action_or_monologue` により、「〜で焼く」「〜食べる」「〜買ってきた」「〜に行く」等の日常行動文や独白（質問表現 `?`, `？`, `何`, `なに`, `とは`, `教えて`, `知ってる` 等を含まない平文）を検出し、URLや明示的検索要求がない限り `has_slang_or_unknown_candidate` / `slang_or_unknown_trigger` / `needs_research` / `should_consult_model` を一括抑制する多重防御を配備すること。
17. **会話文脈の共起固有名詞（Context Co-occurrence）を活用したWeb調査クエリ構築、キャラ台詞パロディ構文（`〇〇「...」`）検知、およびWeb調査確実発火昇格（多重防御、項目23, 136, 174）**:
   - `_CHARACTER_DIALOGUE_QUOTE_RE` および `_has_character_dialogue_meme` により、`キャラ名「台詞」` のなりきり・パロディ構文を検出し、スピーカー名や鉤括弧内の固有名詞（特にカタカナ語や未知語）を抽出すること。
   - `detect_reply_research_rules` において、`_strip_context_speaker_and_meta_labels` を通した本文からのみ共起固有名詞抽出（`context_named_terms`）を行い、話者名プレフィックス、返信先注記、`Assistant` / `User` / `Bot` 等のロールラベル、`詠み手:` 行を除去した上で、最新発言の固有名詞と重複しない上位3語を保持すること。
   - `_GENERIC_TERM_STOPWORDS` に「何年」「何年か」「何年前」「何年か前」「何ヶ月」「何日」「何回」「何個」「何人」「交換」「配布」「保存」「詠み手」「Assistant」「User」「System」「Bot」を登録し、一般疑問語・システムラベルの未知語抽出を遮断すること。
   - 疑問符や明示的時事キーワードを伴わない過去の個人未消化行動報告（例: 「何年か前の〜まだ交換してないよ」等）では、「まだ」単体による `recency_flag` 誤発火を確実に抑止すること。
   - キャラ台詞ネタまたは未知のカタカナ固有名詞が2語以上かつ文脈に固有名詞が存在する場合、`needs_research = True`、`mode_res = "simple_search"` に昇格させ、`_build_context_enriched_query` により最新発言の固有名詞（最大3語）と文脈共起語（最大2語）を結合した検索クエリ（例: `'エニック シックスオー シュエン ナユタ'`）を構築して、作品・文脈の取り違えや知ったかぶり・ハルシネーションを根本遮断すること。
   - 戻り値辞書に `context_named_terms` および `has_character_dialogue_quote` を提供し、上流モジュールとの連携性を担保すること。日常雑談では過剰発火しない安全設計を維持すること。
18. **困りごと・意見募集（相談・解決策模索）の判定と解決策検索クエリ生成（BAN-168遵守）**:
   - `detect_consultation_or_problem_solving_inquiry` により、「〜してる人いない？」「どうすりゃいいんだ」「〜で困ってる」「代わりになる方法ない？」等の困りごと・意見募集（Consultation / Problem-Solving）を検出すること。
   - 体調や感情の単なる独白（「眠くてどうしよう」「疲れて途方に暮れてる」等）は具体名詞・相談マーカーの不在により誤判定を遮断すること。
   - 抽出された主要技術名詞・固有名詞に「代替」「対策」「方法」などの接尾語を適切に付与した解決策検索クエリ（例: `RSS フィード ソフト 自作 代替 方法`）を構築すること。
   - `looks_like_daily_action_or_monologue` の質問マーカーに相談・困りごとキーワードを登録し日常独白としての誤抑制を防止した上で、`detect_reply_research_rules` の `consultation_flag` / `hard_trigger` によりWeb調査を確実に発火させ、表面的な共感（自己投影ミラーリング）に終わらない具体的解決策を提示すること。
19. **Chrome MCP スクリーンショット取得および Vision モデル（Qwen3-VL-8B）画面解析と即時 VRAM 解放規約（BAN-170, BAN-15）**:
   - `chrome_screenshot` ツールにおいて、`url` または既存 `tabId` を受け取り Playwright による高画質 JPEG スクリーンショット（Base64）を取得すること。
   - `describe=True` 指定時は、`OLLAMA_VISION_MODEL`（既定: `qwen3-vl:8b`）に対して `call_ollama(think=False)` で客観的画面説明・文字起こしを実行すること。
   - 推論終了後は `finally` 節で必ず `release_ollama_model(target_model, timeout_sec=5.0)` を呼び出し、VRAM を即時解放すること（BAN-170）。
   - `describe=False` の場合は Vision モデルのロード・呼び出しおよび解放処理をスキップし、Playwright スクリーンショットのみを実行する高速軽量動作を維持すること。
20. **画像検索（search_image）ツール連携と画像コンテキスト検知・リサーチディスパッチ統合（多重防御、BAN-145, BAN-170）**:
   - `search_image` ツールにおいて、画像URL（`image_url`）を受け取り専用逆画像検索API（SauceNAO / trace.moe）並行呼び出しおよび自前ビジョン検証による高精度特定を実施すること。
   - `image_url` が省略された場合でも、直前の会話や添付ファイル（Discord CDN URL、画像拡張子URL、context 内の `attachments` 等）から `extract_image_url_from_context` により自動解決すること。画像URLが特定できずキーワードがある場合はWeb検索へ安全にフォールバックすること。
   - `detect_reply_research_rules` において、「これ誰？」「このキャラ誰？」「誰これ」「この人誰？」「何の画像？」「画像検索」「元ネタ」「出典教えて」等の質問を検知し、文脈内に添付画像、画像解析内容、`[画像]`、または画像URLが存在する場合に `image_search_flag = True`, `needs_research = True`, `mode = "image_search"` を自動解決すること。画像コンテキストがない日常雑談では誤発火させないこと。
   - `resolve_research_mode` において `mode="image_search"` が指定された場合、およびルール判定で `image_search` が解決された場合に、画像URLが含まれていても `browser_read_url` に誤降格させず確実に `"image_search"` を維持・返却すること。
   - `research_dispatch` において `image_search` モードハンドラー `_image_search_dispatch` を配備し、`ImageSearchToolAdapter.search_image_unified` を実行して統一リサーチ結果形式（`used_web=True`, `mode="image_search"`, ソース情報・正規化候補リスト）で返却すること。対象画像URLが特定できない場合は `error="no_image_url"` で安全に早期リターンすること。
   - `register_builtin_tools` において `enable_image_search: bool = True` をオプショナル引数として配備し、既存コードを壊さない後方互換設計を死守すること。
21. **逆画像検索におけるGoogle Lensスクレイピング廃止と専用API・自前ビジョン検証への移行（項目114, 139, 1717）**:
   - Google Lens の Chrome MCP スクレイピング（`chrome_navigate`, `_format_lens_result`, `_is_lens_error_or_wall`）および画像URLファイル名によるWeb検索フォールバック（`_clean_fallback_query`）を全廃し、Bot対策やログイン壁・ハルシネーションを根本排除すること。
   - 構造化JSONを返す SauceNAO（イラスト・二次元画像）および trace.moe（アニメ映像シーン）を並行呼び出しし、共通正規化スキーマ（title, creator, source_url, similarity, thumbnail_url, extra, verdict, confidence, reason, verified）に統一すること。
   - trace.moe API呼び出し時には `anilistInfo=1` パラメータを付与し、APIレスポンスの `anilist` が辞書（`title.native` 等）および整数（AniList ID）のいずれであっても安全にタイトルと出典URL（`https://anilist.co/anime/{id}`）を取り込むこと。
   - 候補判定においてタイトルが空文字列の場合は高類似度であっても `match` にせず `partial`（上限）とし、理由を「タイトル不明のため候補扱い」とすること。
   - `vision_verify` が提供され検証が有効な環境では、検証対象外（上位 N 件の外側）や検証結果未書き込みの候補に残っている `match` 判定をすべて `partial`（理由「検証対象外のため候補扱い」）に格下げし、未検証候補が特定結果になる経路を完全に遮断すること。各候補の検証成否は `verified: bool` で追跡すること。
   - `_build_result_text` において、`verified` が `False` の特定結果には判定理由の末尾に「（ビジョン未検証）」を付与し、検証設定が有効なのに `vision_verify` が未提供の場合は配線漏れ警告ログ（`vision_verify not provided by caller; verification skipped`）を出力すること。
   - 候補が全滅または特定不能な場合は「特定できませんでした」と正直に明記し、`vision_describe` からの手がかりを添え、断定的なハルシネーションを遮断すること。手動確認用リンクとして Lens URL を維持すること。
   - `adapters/image_search.py` および `web_research/dispatch.py` の画像検索処理フロー全体に `[IMAGE_SEARCH]` および `[IMAGE_SEARCH_DISPATCH]` の構造化観測ログを配置し、可観測性を徹底すること。
22. **文脈URL参照判定におけるSystem内部メモURL除外および日常質問誤検知防止（多重防御、BAN-28, BAN-55, BAN-136）**:
   - `_looks_like_context_url_reference` において、「どんな内容だった？」「何の内容？」「どういう内容？」「どんな中身？」「どんな話？」などの一般的な会話質問が、指示代名詞（「その」「これ」「あの」等）や明示的なURL/記事言及（「URL」「リンク」「記事」「ページ」「スレ」等）を伴わない限り文脈URL参照と判定されないようガードを配備すること。また、「内容」「中身」「本文」単体＋疑問符の場合も指示語がなければURL参照から除外すること。
   - `detect_reply_research_rules` において、`context` からURLを抽出する際、`System:` や `(内心のメモ:` を含む行を確実に除外した `clean_context_text` から `context_urls` を抽出すること。内部システムメモ内のURLがユーザーの言及対象として誤認され、消滅URLへのアクセスによる404エラーや目的外のWeb調査（`img_thread` 等）が誤発火する事態を根本遮断すること。
23. **動的ブラウザ対象サイト判定への may.2chan.net 追加およびふたばスレッド先頭URL抽出の保証（多重防御）**:
   - `web_research/config.py` の `_is_dynamic_browser_site` において、動的ブラウザアクセス対象ホスト（`default_hosts`）に `x.com`, `twitter.com`, `img.2chan.net` に加え `may.2chan.net` を登録し、ふたば各板スレッドへの動的ブラウザアクセスを保証すること。
   - `web_research/helpers.py` の `detect_reply_research_rules` における `img_thread_flag` 分岐において、`img_helper._extract_img_thread_urls_full` が未実装のフォールバック時であっても `img.2chan.net` と `may.2chan.net` の両ドメインを対象にスレッドURLを走査・先頭抽出して `query` に設定すること。
24. **未知語候補の重複カウント防止、日常口語カタカナ語除外、および文脈固有名詞からの話者名除外規約（多重防御、項目128, 145, BAN-163）**:
   - `detect_reply_research_rules` において、未知語候補は重複を除いてから数えること（`candidate_term` と `named_terms` の二重計上禁止）。`raw_term_candidates` および `meaningful_unknown_katakana` を `list(dict.fromkeys(...))` により重複排除し、`Character_Dialogue_Or_Context_Entity_Research` の発動条件として「異なる未知カタカナ語が2種類以上」であることを徹底すること。
   - `_COMMON_KATAKANA_STOPWORDS` に日常会話の口語・感情カタカナ表記（「ナイショ」「ヒミツ」「ホント」「ウソ」「ダメ」「ムリ」「イヤ」「スゴイ」「カワイイ」「キレイ」「ヤバイ」「マジ」「ヨロシク」「オネガイ」「ゴメン」「アリガトウ」）を登録し、未知語・スラング候補（`candidate_term` / `slang_or_unknown_trigger`）への誤爆を遮断すること。
   - 文脈固有名詞（`context_named_terms`）の抽出において、コンテキスト内の話者プレフィックス（`[HH:MM] 話者名:` 等）および本文中の呼びかけ（`〇〇さん` 等）から話者・相手呼称を集めて確実に除外し、対話相手名が文脈共起語やWeb検索クエリに混入する事態を防止すること。
25. **未知語ストップワード・愛称保護のConfig駆動・動的合算化（多重防御、項目174, BAN-128, BAN-145）**:
   - `web_research/config.py` の `_GENERIC_TERM_STOPWORDS` から特定キャラクター固有語（「コイト」「コイトフクマル」「福丸小糸」「小糸ちゃん」「小糸」「シャンクス」）のハードコードを削除し、汎用ストップワード（「アシスタント」「AI」「ボット」「bot」等）および一般語・人称代名詞のみを保持すること。
   - `web_research/helpers.py` に `get_generic_term_stopwords() -> set[str]` を配備し、`lib.config_utils.cfg` から現在稼働中のBot表示名（`OLLAMA_BOT_DISPLAY_NAME`）、一人称（`BOT_FIRST_PERSON`）、別名・愛称（`BOT_ALIASES`）を動的に合算して返すこと。
   - `web_research/helpers.py` 内の全ストップワード照合箇所（再検索主要語抽出、既知語キャッシュ登録、特殊未知語判定、好み質問抽出、カギ括弧抽出、固有名詞候補抽出、キャラ台詞パロディ検知、返信前リサーチルール判定）で `get_generic_term_stopwords()` を使用し、反復ループ外での事前合算取得を徹底すること。
26. **web.run 型バッチリサーチツールの仕様、ページレジストリ契約、および多重防御（BAN-1763）**:
   - `web_run`（`adapters/web_run.py`）は OpenAI Codex の `web.run` を参考に、1回のツール呼び出しに複数コマンド（`search_query`, `open`, `find`）を束ね、参照ID（検索結果 `s0`, ページ `p0` 等）および行番号付きでページ本文を返し、`response_length`（`short`/`medium`/`long`）で出力量を制御する。既存の `search_web`（`adapters/web_search.py`）は互換性維持のため一切変更しないこと。
   - `search_query` は最大4件までとし、超過分は切り捨ててログに残すこと。対話相手名誤検索ガード（User-First Resolution）を `search_web` と同様に適用し、対話相手名への誤爆検索を未然に遮断すること。
   - `open` は参照IDが `sN`（検索結果）なら URL または検索キャッシュ本文、`pN`（ページ）なら保持済み本文、URL 直指定なら `browser_read_url` ディスパッチで取得してページ登録すること。`lineno` 指定時は該当行を中心に前後を切り出して返すこと。
   - `find` は対象ページが未取得なら先に取得してから、`page_registry.find_in_page` による決定論的検索（NFKC正規化＋大文字小文字無視、最大20件）で該当行を返すこと。
   - 1コマンドの失敗（ディスパッチ例外・取得不可等）で全体を落とさず、該当コマンド欄にエラーとして明示し他のコマンド結果を返すこと。
   - ページレジストリ（`web_research/page_registry.py`）はプロセス内メモリのみに保持し（永続化なし、常駐タスクなし）、アクセス時に30分経過した期限切れスコープを自動掃除すること。スコープあたりページ20件・検索結果40件の上限（FIFO）および1ページ最大60,000字の切り詰めを徹底すること。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest tests/test_harness_tool_registry.py tests/test_harness_mcp.py tests/test_singing.py harness/agent/tool_registry/tests/test_singing_and_web_research.py harness/agent/tool_registry/tests/test_web_research_katakana_and_puzzle.py harness/agent/tool_registry/tests/test_image_search_adapter.py harness/agent/tool_registry/tests/test_image_search_dispatch.py harness/agent/tool_registry/tests/test_special_action_and_helpers.py harness/agent/tool_registry/tests/test_web_research_fact_check.py harness/agent/tool_registry/tests/test_web_search_partner_guard.py harness/agent/tool_registry/tests/test_browser_navigate_log_and_cleansing.py harness/agent/tool_registry/tests/test_web_research_simulation_and_number_unit.py harness/agent/tool_registry/tests/test_web_research_context_cooccurrence.py harness/agent/tool_registry/tests/test_chrome_mcp_screenshot.py harness/agent/tool_registry/tests/test_reply_research_rules.py harness/agent/tool_registry/tests/test_web_research.py harness/agent/tool_registry/tests/test_research_rule_dedup.py
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/tool_registry
  ```


