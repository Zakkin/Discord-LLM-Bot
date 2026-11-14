# 🤖 Discord Bot 設定・感情・互換シム層 サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`ollama_bot/`）は、**ペルソナ設定（`config_*.py`）、Ollama クライアント（`common/ollama/`）、感情状態・関係値シミュレーション（`ollama_chat_/emotion/`, `ollama_chat_relationships.py`）、および `front/discord/`・`front/hutaba/` への後方互換シム層**を担当するサブプロジェクトです。

> [!NOTE]
> Discord 操作・返信生成・コマンドUIの実装本体は `front/discord/` へ、ふたば（`*.2chan.net`）監視・投稿イベントの実装本体は `front/hutaba/monitor/` へ移設されました。
> Discord 入出力や返信生成を修正する場合は `front/discord/` を、ふたば連携を修正する場合は `front/hutaba/` を開いて作業してください。

---

## 🎯 担当範囲と責務

1. **ペルソナ・Bot設定管理（`config_*.py`, `config_base.py`）**:
   - キャラクタープロンプト・ペルソナ口調・各種パラメータ定義。
   - 環境変数の動的解決（`lib/config_utils.py` 連携、個別プレフィックス優先解決）。
   - マルチBot並行稼働時のコンフィグ単位分離（記憶DBパス・感情状態・Bio設定の自動分離）。
   - 各ペルソナ固有の主力推論モデル管理（ふたば `config_img.py` における `DEFAULT_MAIN_MODEL = "img-local-qwen38-a3b-jpn"` 等）。
   - 感情採点・所感生成の間引き環境設定（`EMOTION_LLM_SCORE_INTERVAL`, `EMOTION_LLM_SCORE_SKIP_PEAK`, `BG_THOUGHT_UPDATE_INTERVAL`。`1` で全通実行のキルスイッチ）。
   - 例外集計レポーター環境設定（`ERROR_REPORT_ENABLED`, `ERROR_REPORT_INTERVAL_SEC`, `ERROR_REPORT_NOTIFY_THRESHOLD` による障害検知・通知間隔・集計閾値管理）。
   - 固有名詞グラウンディング環境設定（`HUTABA_NAME_GROUNDING_ENABLED`, `HUTABA_NAME_GROUNDING_STRIP_FALLBACK`, `DISCORD_NAME_GROUNDING_ENABLED`, `DISCORD_NAME_GROUNDING_STRIP_FALLBACK` による会話文脈外人名・架空助っ人名の遮断とフォールバック制御、既定 True）。
   - 前提・用語・安心断定グラウンディング環境設定（`IMG2CHAN_PREMISE_GROUNDING_ENABLED`, `IMG2CHAN_PREMISE_GROUNDING_STRIP_FALLBACK` / 別名 `HUTABA_PREMISE_GROUNDING_ENABLED`, `HUTABA_PREMISE_GROUNDING_STRIP_FALLBACK` による507型事故対策（相手が言っていない経歴前提の借用、用語崩れ、無条件安心断定の遮断とフォールバック制御）、既定 True）。
   - ペルソナ口調グラウンディング環境設定（`IMG2CHAN_PERSONA_TONE_CHECK_ENABLED`, `IMG2CHAN_PERSONA_TONE_EXEMPT_MAX_CHARS` / 別名 `HUTABA_PERSONA_TONE_CHECK_ENABLED`, `HUTABA_PERSONA_TONE_EXEMPT_MAX_CHARS` によるふたば投稿文生成時のペルソナ語尾喪失・常体化防止と短文免除文字数閾値管理、および `DISCORD_PERSONA_TONE_CHECK_ENABLED`, `DISCORD_PERSONA_TONE_EXEMPT_MAX_CHARS` によるDiscord返信生成時の口調喪失・無個性説明口調遮断と免除文字数管理、既定 True / 20文字）。
   - ペルソナ語尾マーカー環境設定（`PERSONA_TONE_MARKERS` によるペルソナ口調マーカー（語尾・口癖）のタプル設定、既定: `("んですよ", "ですよ", "ますよ", "ですね", "ますね", "んです", "ちゃい", "ちゃう", "ぴゃっ")`）。
   - 行為者照合・出来事メモ環境設定（`ACTOR_CHECK_ENABLED`, `EVENT_MEMO_ENABLED` による出来事メモ `recent_events` および意味ガードによる行為者照合 `actor_mismatch` の有効化/無効化制御、既定 True）。
   - 画像検索・特定環境設定（`IMAGE_SEARCH_SAUCENAO_ENABLED`, `SAUCENAO_API_KEY`, `IMAGE_SEARCH_TRACEMOE_ENABLED`, `IMAGE_SEARCH_VISION_VERIFY_ENABLED`, `IMAGE_SEARCH_MAX_CANDIDATES`, `IMAGE_SEARCH_VERIFY_TOP_N`, `IMAGE_SEARCH_API_TIMEOUT_SEC` による外部API連携・自前Vision判定の共通設定）。
   - 画像探索環境設定（`IMAGE_FIND_ENABLED`, `IMAGE_FIND_MAX_CANDIDATES`, `IMAGE_FIND_MAX_RESULTS`, `IMAGE_FIND_MIN_SCORE`, `IMAGE_FIND_ALLOW_EXPLICIT`, `DANBOORU_LOGIN`, `DANBOORU_API_KEY` による `/image_find` コマンドおよび Danbooru 探索・採点設定）。
   - 自己言及リコール環境設定（`RECALL_ENABLED`, `RECALL_UTTERANCE_LIMIT`, `RECALL_MAX_AGE_SEC`, `RECALL_REFERENCE_NOTE_MAX_CHARS` による言及内容リコール・取得上限・有効期限・言及根拠メモ最大文字数管理）。
   - 行動ログ（bot_activities: 自分の体験の台帳）環境設定（`ACTIVITY_LOG_ENABLED`, `ACTIVITY_LOG_RETENTION_DAYS`, `ACTIVITY_RECALL_MAX_ITEMS`, `ACTIVITY_SUMMARY_MAX_CHARS`, `ACTIVITY_TARGET_TEXT_MAX_CHARS` による全フロント共通行動ログの有効化・保持日数・想起件数・要約最大文字数・対象テキスト最大文字数管理）。
   - 記憶の出所（provenance）環境設定（`MEMORY_PROVENANCE_ENABLED`, `MEMORY_PROVENANCE_QUOTE_MAX_CHARS` による記憶の出所記録・注入・Bot単独事実昇格防止および引用抜粋長管理、既定 True / 120文字）。
2. **Ollama クライアント＆排他制御（`common/ollama/`）**:
   - `call_ollama_chat`, `call_ollama_json`, `call_ollama_caption`, `call_ollama_embeddings` の通信基盤。
   - `call_ollama_json` における切断サルベージ引数 `salvage_comment_on_truncation` の透過的中継と過渡状態フォールバック（下流未対応時の `TypeError` 吸収・再呼び出し）。
   - セマフォ（`OLLAMA_MAX_CONCURRENT_REQUESTS`）によるリクエスト直列化・VRAM排他制御。
   - Embeddingモデル（`bge-m3` 等）の即時解放（`OLLAMA_EMBED_KEEP_ALIVE="0"`）。
   - モデル即時解放関数（`release_ollama_model` / `keep_alive=0`）の提供。
   - Ollama 詳細メトリクス可視化（`[ollama-metrics]` ログ）。
3. **感情状態・関係値シミュレーション（`emotion/`, `ollama_chat_relationships.py`）**:
   - 8基本感情（Plutchikモデル）および複合指標（tension/mood）の算出・更新。
   - 2フェーズ感情更新（Phase 1: 即時ヒューリスティック、Phase 2: バックグラウンドLLM採点）。
   - 会話履歴コンテキスト行の話者分割・リラベル（`_split_context_line` / `relabel_emotion_context_line`）において、行頭の `[HH:MM]` タイムスタンプおよび話者名末尾の関係ラベル（`[独立発言]`, `[>>... への返信]` 等）を除去し、`相手(話者): 本文` や `あなた: 本文` を正確に整形。
   - 感情理由テキストにおける非日本語CJK（頻出簡体字・中国語フレーズ）の混入検知（`contains_cjk_non_japanese`）によるプロンプト健全性の維持。
   - ユーザー関係性モデル（好意度・親愛度・警戒度、および恋人 `is_lover` ステータスの永続化）。
   - ユーザー関係値・感情ログ・所感の物理除去・即時アトミック保存API（`purge_user_relationship_state`）。
   - サーバー別メンバー自己紹介（Bio）同期（`PATCH /guilds/{guild_id}/members/@me` による独立管理）。
   - 生活リズム（静かな時間帯: `BOT_QUIET_HOURS_ENABLED`）連動による Presence ステータス（`discord.Status.idle` / `online`）の自動制御。
   - 感情採点・所感生成の間引き制御（`EMOTION_LLM_SCORE_INTERVAL`、`EMOTION_LLM_SCORE_SKIP_PEAK`、`BG_THOUGHT_UPDATE_INTERVAL` による応答遅延抑制とスラッシング防止、`1` 指定で全通実行のキルスイッチ対応）。
   - 対話ごとのユーザー関係ログの肥大化防止（`scoring_mixin.py` における `trim_old_user_relationship_logs` 自動呼び出しによる直近50件保持）。
4. **後方互換シム層の保守（`ollama_chat_/` 配下）**:
   - `front/discord/` への移管に伴い設置された薄い互換シム（委譲モジュール）の整合性維持。
   - 外部・既存テストからの参照互換性の保証。

---

## 🚫 境界条件と禁止事項（最重要）

1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `ollama_bot/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`front/discord/`, `front/hutaba/`, `harness/agent/*` 等）のファイルを直接変更してはならない。
2. **単一プロセス・独立Bot稼働の厳守（項目9, 122, 124）**:
   - 同一トークンでの複数プロセス同時起動を絶対に禁止する。
   - 自己紹介（Bio）更新は全サーバー共通の `PATCH /applications/@me` ではなく、担当ギルド（`resolve_bot_guild_id()`）を特定して `PATCH /guilds/{guild_id}/members/@me` へ送信すること（同一トークン下での他Bot・他サーバーBio相互上書きの防止）。
3. **人格・特定台詞のハードコード根絶（項目5, 11）**:
   - 特定キャラクター名や特定の口癖・台詞をコード内にハードコードせず、すべて `config_*.py` 駆動とすること。感情理由プロンプト（`prompts_mixin.py`）等のフォールバック値においても特定キャラ名（コイト等）を直書きせず、汎用的一人称（`"私"`）に統一すること。
4. **Ollama推論・VRAM解放ライフサイクルの厳守（項目15, 144, 180）**:
   - ラッパー関数の `think` パラメータは `bool | None = None` とし、設定値および動的Thinking判定（`_should_enable_adaptive_thinking`）が確実に適用されるようにすること。
   - Visionモデル（`qwen3-vl`）やEmbeddingモデル（`bge-m3`）使用後は、`release_ollama_model`（`keep_alive=0`）で即座にVRAMをアンロードし、メインモデル推論用のVRAMを確保すること。
   - メインモデル推論直前には `OLLAMA_MIDDLE_MODEL` などのユーティリティモデルを事前アンロードしてVRAMスラッシングを防止すること。
5. **感情状態・関係性モデルの安全性と永続化（項目1, 5, 22, 160, 177）**:
   - 会話履歴のコロン分割（`_split_context_line`）時にタイムスタンプ `[HH:MM]` で誤分割して `相手([22): 00] ...` と破壊しないこと。また話者名末尾の関係ラベル（`[独立発言]` 等）を除去して正しく話者を特定すること。
   - 所感・理由生成における簡体字混入（`劲儿` や頻出簡体字）を `contains_cjk_non_japanese` で確実に検知・遮断すること。
   - `anticipation`（期待・好意）単独で `tension`（警戒・緊張度）を高騰させないこと（好意的な会話で敵対的警戒状態 `mood=tense` が誤注入されるのを防止）。
   - パートナー関係における恋人ステータス（`is_lover: bool`）は再起動で消失しないよう JSON に完全永続化すること。
   - 生活リズム（静かな時間帯: `BOT_QUIET_HOURS_ENABLED`）が有効かつ静かな時間帯内の場合、Presence Activity を `discord.Status.idle` に連動させること。
   - ユーザー記憶忘却（`/memory forget` 等）連携時、対象ユーザーの関係値や感情キャッシュを `purge_user_relationship_state` でアトミックかつ安全に除去・永続化し、不整合を残さないこと。
   - 親密な相手に対する冷淡なAIメタ発言（「私はAIですので」等）を禁止すること。
6. **後方互換シムの破壊禁止**:
   - `ollama_chat_/` 配下のシムファイルは既存テストや外部呼び出し元が参照しているため、独断でシグネチャを変更したり削除しないこと。
7. **サブチャンネル（草むら）割り込み判定の健全性**:
   - 数字・記号・顔文字のみ（意味の取れる語がない）の他ユーザー宛て返信は、サイコロ（抽選確率）を消費せずスキップする（`OTHER_CHANNEL_SKIP_LOW_INFO_REPLY`、既定 True）。Bot宛て返信や添付ファイル付き、および独立発言は除外し、不自然な割り込み返信を防止すること。

---

## 🛠️ 検証手順
```bash
PYTHONPATH=mock_libs:. pytest ollama_bot/tests/
PYTHONPATH=mock_libs:. mypy --config-file mypy.ini ollama_bot
```
