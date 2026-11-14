# Ollama Bot (`ollama_bot/`)

ペルソナ設定（`config_*.py`）、Ollama クライアント（`common/ollama/`）、感情状態・関係値シミュレーション（`ollama_chat_/emotion/`）、および `front/discord/` への後方互換シムを担当するサブプロジェクトです。

## 構成
- `config_base.py`: 全ペルソナ共通の基本設定およびデフォルト環境変数定義
- `config_img.py`: ふたば（img）連携ペルソナ設定（デフォルト推論モデル: `img-local-qwen38-a3b-jpn`）
- `config_loader.py` / `config_sample.py`: 設定ローダーおよび設定サンプル
- `common/ollama/`: Ollama API クライアント、トークン推定、モデル制御
- `ollama_chat_/`: 感情状態（`emotion/`）および旧パス互換シム層。感情採点プロンプト構築時のタイムスタンプ（`[HH:MM]`）付き会話履歴の安全な話者分割・正規化（`_split_context_line`）、簡体字混入検出（`contains_cjk_non_japanese`）、第三者宛て返信時感情理由プロンプトにおける汎用的一人称（`"私"`）フォールバック統一、およびユーザー関係ログの自動トリム（直近50件保持）を包含。

## 主要環境変数・設定一覧

### ふたば（img2chan）連携・Discord誘導設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMG2CHAN_PROMOTION_ENABLED` | `false` | ふたば自律投稿時におけるDiscord招待URL自動誘導の有効化フラグ（**安全のため既定値は無効**）。明示的に `"true"` を指定した場合のみ有効化されます。 |
| `IMG2CHAN_PROMOTION_PROBABILITY` | `0.05` | 誘導メッセージが付与される確率（5%） |
| `IMG2CHAN_PROMOTION_MIN_INTERVAL_SEC` | `21600` | 誘導メッセージ付与の最小インターバル（秒、既定: 6時間） |
| `IMG2CHAN_PROMOTION_INVITE_URL` | 各ペルソナ設定準拠 | Discord招待リンクURL |
| `IMG2CHAN_PROMOTION_MESSAGE` | 各ペルソナ設定準拠 | 投稿末尾に付加される案内文面 |

### 自発発話・モデル設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `AGENT_PROACTIVE_POST_ENABLED` | `true` | 自発発話（会話が途切れた際の自律発話）の有効/無効 |
| `OLLAMA_MAIN_MODEL` | ペルソナ準拠 | メイン推論用モデル名（ふたば `config_img.py` 既定値: `img-local-qwen38-a3b-jpn`） |
| `OLLAMA_NUM_CTX` | `16384` | コンテキスト長上限 |
| `OLLAMA_EMBED_KEEP_ALIVE` | `0` | 埋め込みモデル（bge-m3等）使用後の即時アンロード設定（VRAM競合・メインモデルCPUオフロード防止） |

### 2段階Visionパイプライン設定（画像読み取り専用前処理とVRAM即時解放）
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `OLLAMA_VISION_MODEL` | `qwen3-vl:8b` | 画像解析・キャプション生成用Visionモデル名 |
| `OLLAMA_VISION_KEEP_ALIVE` | `0` | Visionモデル推論完了後のアンロード待機時間（即時解放） |
| `OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC` | `60.0` | 画像キャプション生成のタイムアウト秒数（Cold Start対応） |
| `OLLAMA_VISION_NUM_CTX` | `4096` | Visionモデルのコンテキスト長（画像解析・プロンプト用） |
| `OLLAMA_VISION_NUM_PREDICT` | `256` | 画像説明・文字起こしの最大出力トークン数 |
| `OLLAMA_VISION_TEMPERATURE` | `0.1` | 客観的描写のための生成温度（低温度） |
| `VISION_PIPELINE_MODE` | `caption_then_main` | 画像処理パイプラインモード |

### サブチャンネル（草むら）応答・割り込み設定
| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `OTHER_CHANNEL_SKIP_LOW_INFO_REPLY` | `true` | 他ユーザー宛ての返信で、本文が数字・記号・顔文字のみ（意味の取れる語がない）の場合にサイコロを消費せずスキップする設定（bool） |

### 感情採点・所感生成の間引き制御設定
毎メッセージ直列実行による応答遅延とVRAMスラッシングを防止するため、感情のLLM採点および背景所感生成の間引き頻度を制御します。各インターバルに `1` を指定すると毎メッセージ全通実行（キルスイッチ）となります。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `EMOTION_LLM_SCORE_INTERVAL` | `3` | 感情のPhase 2（バックグラウンドLLM採点）を実行するメッセージ間隔（通）。`1` で全通実行（間引き無効）。 |
| `EMOTION_LLM_SCORE_SKIP_PEAK` | `0.20` | 間引き判定時、ヒューリスティック感情変動の最大ピーク値がこの閾値を超えた場合は強制的にLLM採点を実行する安全閾値。 |
| `BG_THOUGHT_UPDATE_INTERVAL` | `2` | 相手に対する所感（バックグラウンド思考）を更新するメッセージ間隔（通）。`1` で全通実行（間引き無効）。 |

### 例外集計レポーター設定
障害発生時に例外がサイレントに握りつぶされて長期間再発に気づけない事態を防ぐため、集計ウィンドウや通知閾値を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `ERROR_REPORT_ENABLED` | `true` | 例外集計レポーターの有効化フラグ（`true` / `false`） |
| `ERROR_REPORT_INTERVAL_SEC` | `300` | 例外集計・通知インターバル（秒、既定: 300秒 / 5分） |
| `ERROR_REPORT_NOTIFY_THRESHOLD` | `20` | 通知対象とする同一例外発生件数の閾値（回、既定: 20回） |

### 会話破綻防止・訂正取り込み・状態管理設定
誤断定・否定無視・主語取り違えによる会話破綻を防ぐ6機能の有効化フラグおよび各種閾値・上限値の設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `CORRECTION_UPTAKE_ENABLED` | `true` | 改善1: 訂正の取り込み（否定された主張の撤回・封印・毒抜き）の有効化フラグ |
| `DENIED_CLAIMS_MAX` | `10` | 否定された主張・前提の保持上限数（件） |
| `DENIED_CLAIMS_TTL_SEC` | `21600.0` | 否定された主張の有効期限（TTL、秒、既定: 6時間） |
| `REPLY_SEMANTIC_GUARD_ENABLED` | `true` | 改善2: 送信前チェックの意味レベル化（再主張・主語取り違えの差し戻し）の有効化フラグ |
| `REPLY_SEMANTIC_GUARD_MAX_RETRIES` | `1` | 送信前意味検証失敗時の最大リトライ回数 |
| `FRUSTRATION_GUARD_ENABLED` | `true` | 改善3: 苛立ちの検出と引き際（クールダウンによる泥沼化防止）の有効化フラグ |
| `FRUSTRATION_TRIGGER_CORRECTIONS` | `2` | クールダウン発動トリガーとなる連続訂正・苛立ち回数 |
| `FRUSTRATION_COOLDOWN_SEC` | `600.0` | 苛立ち検知時のクールダウン時間（秒、既定: 10分） |
| `CONVERSATION_STATE_ENABLED` | `true` | 改善4: 会話状態メモ（ターン単位の前提固定）の有効化フラグ |
| `CONVERSATION_STATE_UPDATE_EVERY_TURNS` | `4` | 会話状態メモを更新する会話ターン間隔 |
| `CONVERSATION_STATE_MAX_FACTS` | `8` | 保持する会話前提事実の最大数 |
| `CONTEXT_COMPACT_ENABLED` | `true` | 改善5: 文脈の棚卸し（古い生ログの圧縮と錨の除去）の有効化フラグ |
| `CONTEXT_COMPACT_TRIGGER_LINES` | `40` | 文脈要約・圧縮を実行するトリガー行数 |
| `CONTEXT_COMPACT_KEEP_RECENT` | `12` | 要約時に未圧縮のまま保持する直近の生発言行数 |
| `CORRECTION_LEARNING_ENABLED` | `true` | 改善6: 訂正エピソードの学習活用の有効化フラグ |
| `ACTOR_CHECK_ENABLED` | `true` | 行為者取り違え対策: 意味ガードによる行為者照合（`actor_mismatch`）の有効化フラグ |
| `EVENT_MEMO_ENABLED` | `true` | 行為者取り違え対策: 出来事メモ（`recent_events`）の有効化フラグ |

### 固有名詞グラウンディング設定（架空人名・固有名詞捏造防止）
会話やスレッドに存在しない架空の外国人名・固有名詞（例: 「アダム・リゲイ」等の捏造）をモデルが出力してしまう事故を防止するため、前処理・後処理の決定論的グラウンディングチェックを制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `HUTABA_NAME_GROUNDING_ENABLED` (`IMG2CHAN_NAME_GROUNDING_ENABLED`) | `true` | ふたば投稿前の固有名詞グラウンディング検証有効化フラグ（スレ外人名・架空助っ人名の遮断） |
| `HUTABA_NAME_GROUNDING_STRIP_FALLBACK` (`IMG2CHAN_NAME_GROUNDING_STRIP_FALLBACK`) | `true` | ふたば側でグラウンディング検証により全候補が失格となった場合、問題の固有名詞を含む文を除去してフォールバック送信するフラグ |
| `DISCORD_NAME_GROUNDING_ENABLED` | `true` | Discord返信時の固有名詞グラウンディング検証有効化フラグ（会話文脈外人名・架空固有名詞の遮断） |
| `DISCORD_NAME_GROUNDING_STRIP_FALLBACK` | `true` | Discord側でグラウンディング検証により全リトライが失格となった場合、問題の固有名詞を含む文を除去してフォールバック送信するフラグ |

### 前提・用語・安心断定グラウンディング設定（507型事故対策）
ふたば（`*.2chan.net`）投稿文生成において、相手が述べていない経歴前提の借用、用語崩れ、無条件安心断定によるハルシネーション（いわゆる「507型事故」）を防止するため、投稿前のグラウンディング検証を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `HUTABA_PREMISE_GROUNDING_ENABLED` (`IMG2CHAN_PREMISE_GROUNDING_ENABLED`) | `true` | ふたば投稿前の前提・用語・安心断定グラウンディング検証有効化フラグ |
| `HUTABA_PREMISE_GROUNDING_STRIP_FALLBACK` (`IMG2CHAN_PREMISE_GROUNDING_STRIP_FALLBACK`) | `true` | グラウンディング検証により全候補が失格となった場合、問題の前提・断定文を除去してフォールバック送信するフラグ |

### ペルソナ口調グラウンディング設定（口調喪失・常体化対策）
ふたば（`*.2chan.net`）投稿文生成およびDiscord返信文生成において、ペルソナ固有の語尾や口調が失われ無個性・説明口調や匿名掲示板の常体（〜だな、〜だろな等）になる事故を防止するため、投稿・返信前の口調検証および短文免除文字数閾値を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMG2CHAN_PERSONA_TONE_CHECK_ENABLED` (`HUTABA_PERSONA_TONE_CHECK_ENABLED`) | `true` | ふたば投稿前のペルソナ口調チェック有効化フラグ |
| `IMG2CHAN_PERSONA_TONE_EXEMPT_MAX_CHARS` (`HUTABA_PERSONA_TONE_EXEMPT_MAX_CHARS`) | `20` | ふたば側で口調チェックを免除する短文の最大文字数閾値（文字、既定: 20文字以下の短文はチェック対象外） |
| `DISCORD_PERSONA_TONE_CHECK_ENABLED` | `true` | Discord返信前のペルソナ口調チェック有効化フラグ（口調喪失・無個性説明口調の遮断） |
| `DISCORD_PERSONA_TONE_EXEMPT_MAX_CHARS` | `20` | Discord側で口調チェックを免除する短文の最大文字数閾値（文字、既定: 20文字以下の短文はチェック対象外） |
| `PERSONA_TONE_MARKERS` | `んですよ,ですよ,ますよ,ですね,ますね,んです,ちゃい,ちゃう,ぴゃっ` | ペルソナ口調マーカー（語尾・口癖）のカンマ区切り設定（カンマ区切りでパースされタプル化） |

### ふたば（img2chan）文脈画像キャプション設定
ふたば（`*.2chan.net`）スレッドの文脈に含まれる画像に対するキャプション生成を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMG2CHAN_CONTEXT_IMAGE_CAPTION_ENABLED` (`HUTABA_CONTEXT_IMAGE_CAPTION_ENABLED`) | `true` | ふたば文脈画像に対するオンデマンドキャプション生成の有効化フラグ（`IMG2CHAN_*` 未設定時は別名 `HUTABA_*` を参照） |

### 画像検索・特定（SauceNAO / trace.moe / 自前Vision判定）設定
画像元の特定（アニメ・イラスト・マンガ等）を行うための外部APIおよび自前Vision検証に関する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMAGE_SEARCH_SAUCENAO_ENABLED` | `true` | SauceNAO API による画像検索・特定の有効化フラグ |
| `SAUCENAO_API_KEY` | `""` | SauceNAO API キー（未設定時は制限付きまたはスキップ） |
| `IMAGE_SEARCH_TRACEMOE_ENABLED` | `true` | trace.moe API によるアニメシーン特定の有効化フラグ |
| `IMAGE_SEARCH_VISION_VERIFY_ENABLED` | `true` | 検索候補画像を自前Visionモデルで照合・ファクトチェックする検証フラグ |
| `IMAGE_SEARCH_MAX_CANDIDATES` | `5` | APIから取得する最大検索候補数（件） |
| `IMAGE_SEARCH_VERIFY_TOP_N` | `3` | Visionモデルで詳細照合を行う上位候補数（件） |
| `IMAGE_SEARCH_API_TIMEOUT_SEC` | `15.0` | 画像検索APIリクエストのタイムアウト時間（秒） |

### 画像探索（/image_find / Danbooru + Vision採点）設定
Danbooru APIを用いた画像探索およびVisionモデルによる適合度採点を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `IMAGE_FIND_ENABLED` | `true` | 画像探索機能（`/image_find`）の有効化フラグ |
| `IMAGE_FIND_MAX_CANDIDATES` | `12` | Danbooru から取得する初期候補画像数（件） |
| `IMAGE_FIND_MAX_RESULTS` | `5` | ユーザーに提示する最終探索結果の最大件数（件） |
| `IMAGE_FIND_MIN_SCORE` | `60` | Vision採点による採用最低スコア（点、0〜100） |
| `IMAGE_FIND_ALLOW_EXPLICIT` | `false` | R-18/過激画像の取得・表示許可フラグ（安全のため既定値は無効） |
| `DANBOORU_LOGIN` | `""` | Danbooru ログインユーザー名（API認証用） |
| `DANBOORU_API_KEY` | `""` | Danbooru API キー（API認証用） |

### 自己言及リコール（言及した内容を思い出す機能）設定
自発投稿生成時に外部コンテンツ（Xタイムライン等）の言及根拠メモを保持し、過去の発言内容を思い出すリコール機能を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `RECALL_ENABLED` | `true` | 自己言及リコール機能の有効化フラグ |
| `RECALL_UTTERANCE_LIMIT` | `20` | リコール対象とする過去言及履歴の最大取得件数（件） |
| `RECALL_MAX_AGE_SEC` | `86400.0` | リコール対象とする発言の有効期限（秒、既定: 24時間） |
| `RECALL_REFERENCE_NOTE_MAX_CHARS` | `800` | 保持する言及根拠メモの最大文字数（文字） |

### 行動ログ（bot_activities: 自分の体験の台帳）設定
Bot自身の行動（投稿・返信・リアクション・閲覧・調査）を全フロント共通台帳へ記録・想起する機能を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `ACTIVITY_LOG_ENABLED` | `true` | 行動ログ機能（台帳記録および想起）の有効化フラグ |
| `ACTIVITY_LOG_RETENTION_DAYS` | `180` | 行動ログの保持期間（日、既定: 180日） |
| `ACTIVITY_RECALL_MAX_ITEMS` | `3` | 会話文脈に応じて想起・注入する過去行動の最大件数（件） |
| `ACTIVITY_SUMMARY_MAX_CHARS` | `400` | 行動ログ要約（サマリー）の最大文字数（文字） |
| `ACTIVITY_TARGET_TEXT_MAX_CHARS` | `80` | 行動対象テキスト（抜粋）の最大文字数（文字） |

### 記憶の出所（provenance）設定
Bot自身の過去発言や創作を客観的事実として誤昇格させることを防ぎ、出所（相手発言・Bot発言・外部出典）の記録・注入・引用抜粋を制御する環境設定です。

| 環境変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `MEMORY_PROVENANCE_ENABLED` | `true` | 記憶の出所（provenance）記録・注入およびBot単独発言の事実昇格防止の有効化フラグ |
| `MEMORY_PROVENANCE_QUOTE_MAX_CHARS` | `120` | 記憶の根拠発言を引用・抜粋する際の最大文字数（文字） |




### メッセージ受信・返信トリガー仕様（画像付きメッセージ対応）
- **画像付きメッセージ・画像単体メッセージの返信許可**:
  - 画像（添付ファイルまたは画像URL）が含まれるメッセージは、テキストの有無にかかわらず無視されず返信対象となります。
  - テキストが空であっても、画像またはサポート対象メディアが含まれていれば2段階Visionパイプライン経由で画像内容（キャプション・文字起こし）が抽出され、Bot返信が生成されます。
  - テキスト・画像・サポート対象メディアが一切存在しない完全な空メッセージのみが無視（スキップ）されます。

### 構造化JSON出力呼び出し（`call_ollama_json`）と切断サルベージ仕様
- `common/ollama/structured.py` の `call_ollama_json` は、下流パーサー（`harness/agent/prompt_parser/`）へのプロキシとして機能します。
- `salvage_comment_on_truncation: bool = False` 引数を受け取り、トークン上限等により途中で切断されたJSONからコメントを救出（サルベージ）する指示を下流へ中継します。
- 下流のパーサーが未対応である過渡状態（並行開発時等）では、`TypeError` を自動検知して引数を除外し再試行する多重防御（フォールバック）を備えています。

## 型検査
```bash
mypy --config-file mypy.ini ollama_bot
```
