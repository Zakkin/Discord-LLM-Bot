# Front Discord (`front/discord/`)

Discord を操作するスクリプト群（Bot 本体・Cog・イベント処理・返信生成・コマンド UI）を格納するサブプロジェクトです。

## 構成
- `main.py` / `cog.py`: Discord Bot エントリポイントと `OllamaChatCog`
- `adapter.py`: `DiscordAdapter`（`discord.Message` ⇄ `FrontEvent` / `AgentTask`）
- `events.py` / `research_events.py`: イベント受信・調査連携・バックグラウンドタスク起動管理（感情減衰・習慣学習・定期内省・ふたば自発巡回 `_img2chan_proactive_post_task` 等）
- `reply/`: 返信分類・通常返信・割り込み返信生成・ユーザー返信最優先譲歩（`_yield_to_user_replies`）
- `admin.py`: Bot稼働制御（`/bot`, `/bot_next_reply`）およびスラング辞書管理（`/slang`）
- `commands/`: 歌唱・20の扉・ウミガメのスープ・画像検索
- `lifecycle/`: モデル呼び出し・調査ライフサイクル
- `common/` / `utils.py`: Discord メッセージ・メディア・Bot 識別ヘルパー

## コマンド一覧
- **`/bot` (`start` / `stop` / `status`)**: Botの応答機能の起動・停止・ステータス確認（管理者専用操作あり）
- **`/bot_next_reply`**: 返信カウントを上限に引き上げ、次回レスへ確定即時返信（管理者専用）
- **`/slang` (`action:learn|show|list`, `term:<単語>`)**:
  - `learn`: 未知のスラングをWeb検索＆LLM構造化解析し、`memory.db` に自動判定・登録（管理者専用）
  - `show`: 登録済みスラングのカテゴリ、検索除外設定、固有名詞判定、意味、Botガイダンス注記をEmbed表示
  - `list`: 登録済みスラング辞書の一覧と総件数をEmbed表示
- **`/image_search` / アプリメニュー「画像検索」**:
  - 指定された添付画像、画像URL、またはチャンネル直近履歴（過去10件）/右クリック対象メッセージから画像URLを検出し、画像検索（Google Lens等）で調査した結果をEmbedで表示。
  - 検索結果Embedのフッターは特定キャラ名直書きではなく、設定（`OLLAMA_BOT_DISPLAY_NAME` / `BOT_FIRST_PERSON`）を動的に反映（例: `{名前}画像調査システム`、未設定時は `"画像調査システム"`）。
- **歌唱・ゲーム**: `/sing`（歌唱）, `/twenty_doors`（20の扉）, `/umigame`（ウミガメのスープ）

## 自律タスク・優先度連携
- `events.py` の `_ensure_background_tasks_started` により、`HUTABA_PROACTIVE_POST_ENABLED` に基づきふたば自発巡回タスク（`_img2chan_proactive_post_task`）が安全に起動管理されます。
- バックグラウンドタスク（ふたば自発巡回、前ターンの長期記憶抽出 `extract_and_store_memory`、関係性一言所感生成等）は、Discord ユーザー返信が推論中（`_active_user_reply_count > 0`）だけでなく、前処理（Web調査・文脈判定・事前分析）中（`_active_user_reply_turn_count > 0`）やキュー待機中（`not _reply_queue.empty()`）を含むビジー状態（`_is_user_reply_busy()`）の間、`_yield_to_user_replies` により安全に待機・譲歩し、Discord 返信を最優先するリソース競合制御を行います。
- `_yield_to_user_replies` 内では、完了イベント（`_user_reply_done_event`）がセット済みのままカウントが残る状況でもビジーループによるイベントループ占有（Discord Gateway Heartbeat 遮断）が発生しないよう、`event.is_set()` ガードと必須の協調スリープ（`await asyncio.sleep(0.2)`）を保証しています。また、キュー投入時、`_process_queued_message` の開始・終了、および各推論呼び出し（`_call_with_typing`, `_call_chat_with_typing`, `_discord_llm_chat`）において `_refresh_user_reply_done_event()` を呼び出し、未完了ターンやキュー待機メッセージがある場合に完了イベントを誤ってセットしないライフサイクルを統一しています。
- 会話履歴（`context_lines`）からの話者名抽出時に `[HH:MM]` や `[HH:MM:SS]` の先頭タイムスタンプを除去してから分割することで、話者名が `[22` などに誤分割される不具合を防止しています。
- Web調査判定（`reply_research_decider`）でモデルが返した検索クエリから、ユーザー発言本文に含まれていないロール名（`Assistant`, `User`, `System`, `Bot` 等）や対話相手の名前を自動除去し、検索クエリの汚染を防止しています（BAN-163, BAN-174）。

## 複数人雑談・Recency Bias 防止機構
- **親子関係・独立発言の構造化ラベル付け (`format_discord_context_line_with_relation` / `format_context_lines_with_relation_labels`)**:
  - `context_lines` の各行に `[>>ユーザー名 への返信]` または `[独立発言]` を明示し、複数人雑談での会話ツリーの混同を防止。
- **直前メッセージ境界ガード (`build_different_author_boundary_note` / `_apply_different_author_boundary_note`)**:
  - 返信対象の直前に無関係な別ユーザーの発言が挟まった場合、直前発言の語彙を引きずるRecency Biasを防ぐための境界注記をプロンプトへ注入。先行会話履歴（`prior_context_items`）がある場合は「直前の話題が今回の話題に繋がる」等のキメラ合成（無理な関連付け）を厳禁とし、直前発言より前の文脈の続きとして応答するよう指示。
- **System内心メモの除外とキャッシュ適正化 (`discord_helpers.py`)**:
  - 自発発言直後にキャッシュされる `role="system"`（`System: (内心のメモ: ...)`）は人間の発言ではないため、`build_different_author_boundary_note` および `_apply_different_author_boundary_note` において別ユーザーとして誤認されないようガードし、直前の実際の発話（人間またはBot）を参照。
  - `find_reply_parent_from_cache` においても親候補から `System` 内心メモを除外し、存在しない `target_message_id` が指定された場合は安全に `None` を返却。
  - `recent_context_items` では期限切れの `system` メモを `min_keep` で保持せず、有効期間内のSystemメモも最新の1件のみに限定して、数時間前の長大な内心メモによるプロンプト圧迫を防止。
- **話題切り出し短文の同一話者バースト連投集約 (`looks_like_incomplete_topic_opener` / `collect_same_author_burst_texts`)**:
  - 『〇〇さあ』『〇〇ってさ』『〇〇なんだけど』のような未完結な話題切り出し短文で返信トリガーが発火した際、直後に同一話者が連投したキャッシュ内の後続メッセージ（2行目・3行目等）を自動待機・回収して改行結合し、先走り返信や文脈切断を防止。
- **メタ無知表明サニタイズ (`looks_like_meta_ignorance_reply` / `sanitize_meta_ignorance_reply`)**:
  - 「置いてけぼりになっちゃいます」「急に言われても分からない」等のメタな無知・疎外感表明を検知し、等身大の共感・相槌へ自動置換。

## 返信生成パイプラインと VRAM 最適化（GPU / CPU オフロード制御）
- **メインモデル実行直前のユーティリティモデル自動解放 (`_call_reply_model_or_fallback`)**:
  - 事前分析（`middle-llm`）やWeb調査判定完了後、メインモデル呼び出し直前に `release_ollama_model` により `middle-llm` をアンロード。
  - Tesla T4 などの VRAM 16GB GPU環境において、複数モデルの同時滞留による VRAM 競合やメインモデルの CPU オフロード（70%）低速固定化を防止し、メインモデルが利用できる VRAM 空間（最大GPUオフロード）を確実に確保します。
- **タイムアウト例外の安全な処理とモデル解放 (`research_events.py`)**:
  - `ReplyModelTimeoutError` 発生時、例外ハンドラ内で確実に `_release_middle_model_if_needed()` を実行してからフォールバック返信を送信し、正常系の処理フローを阻害しない安全なライフサイクル制御を行います。
- **2段階Visionキャプション・パイプライン (`describe_images_with_vision_model` / `build_message_media_context`)**:
  - 画像添付時に返信生成モデル全体をVisionモデルに差し替えるのではなく、前処理として `describe_images_with_vision_model` により画像の説明・文字起こしのみを抽出し、直後に `release_ollama_model` でVisionモデルをGPU VRAMからアンロード。
  - キャプション抽出成功時はメインモデル（テキストLLM）に `images=[]` で画像解析テキスト（`analysis_text`）を渡すことで、メインモデルのペルソナや口調を維持したまま画像内容に即した返答を実現し、GPU VRAMの競合（CPUオフロード低速化）も防止します。
  - タイムアウト初期値はCold Start（モデルロード＋画像エンコード＋推論）に耐えられるよう `60.0` 秒（`OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC`）に拡張され、KVキャッシュの確保負荷を最適化するためコンテキストサイズは `4096`（`OLLAMA_VISION_NUM_CTX`）に設定されています。
  - 万が一画像が残存した場合でも、`_call_model` で `VISION_PIPELINE_MODE == "caption_then_main"` かつ非Visionモデルなら画像を切り離してメインモデルを呼び出す多重防御を備えています。

## 返信先画像の自動解決と画像特定機能（User-First Resolution 連携）
- **リプライ先（reference）画像の自動フェッチ (`resolve_message_with_media`)**:
  - 画像付きメッセージに対して「これ誰？」「元ネタ教えて」等のテキスト単体リプライが送られた場合、返信元（`message.reference`）から画像を自動解決（キャッシュまたは `channel.fetch_message` による非同期取得）。Vision キャプション生成および外部画像検索への画像伝搬を可能にします。
- **User-First Resolution における画像検索バイパス保護**:
  - 画像検索リクエスト（`mode == "image_search"` または `image_search_request`）の場合、サーバーメンバー名（例: 『雑菌』さん）への単語一致があっても外部調査キャンセル（`BLOCKED`）をバイパスし、正常に画像検索（Google Lens等）を実行します。
- **画像検索・調査の構造化ロギング (`image_search.py`, `research_events.py`)**:
  - コマンド受付（`[IMAGE_SEARCH_CMD]`）、コンテキストメニュー呼び出し（`[IMAGE_SEARCH_MENU]`）、検索実行・結果（`[IMAGE_SEARCH_EXEC]`）、および返信リサーチ時解決（`[IMAGE_SEARCH_EVENT]`）の各フェーズで構造化ログを出力し、コンソールおよび `bot.log` での追跡性と結果表示の堅牢性を担保。
- **相談・自己投影ミラーリング検知の動的Config化 (`research_events.py`)**:
  - 困りごとや意見募集に対する自己投影逃げ（「私も〜で困ったことあるんですよ」等）の検知において、特定キャラ名のハードコードを廃止。
  - 標準一人称（私、僕、俺、自分、うち等）に加えて `BOT_FIRST_PERSON` や `OLLAMA_BOT_DISPLAY_NAME` を動的に反映し、マルチペルソナ環境でも確実に自己投影逃げを検知・再生成リトライを実行。

## Web調査失敗時の会話フォールバックとコンテキストサニタイズ (`research_events.py`)
- **会話型質問へのLLMフォールバック制御**:
  - Web調査が HTTP 404 やタイムアウト等で失敗（不足）した場合でも、ユーザーが会話型の質問（「どんな内容だった？」等）をしている場合は、機械的な「404で取れなかった」定型文で会話を打ち切らず、手元の会話コンテキストを踏まえた通常LLM推論（キャラクターらしい自然な返答）へ安全にフォールバックします。
  - 明示的なURL調査命令（「URL」「リンク」「このサイト」「そのサイト」等）やURL単体投稿の場合は、引き続き失敗定型文を返します。
- **調査用コンテキストのサニタイズ (`_build_research_context_text`)**:
  - 調査用コンテキスト構築時に `System:` や `内心のメモ:` を含む行を除外し、内部情報や不要なURLの漏洩・誤検知を遮断します。

## ふたばスレッド調査連携（may.2chan.net 対応・複数URL先頭優先）(`research_events.py`)
- **`may.2chan.net` スレッドURL判定連携**:
  - `img.2chan.net` に加え、`may.2chan.net` のスレッドURL（`https://may.2chan.net/b/res/*.htm`）が入力された場合でも、調査判定で汎用ブラウザ読み（`browser_read_url`）への誤流出を防ぎ、確実に `img_thread` モードとして解決・整形返信します。
- **複数URL指定時の先頭1件限定処理**:
  - ユーザー入力やコンテキストに複数のスレッドURLや汎用URLが混在している場合、先頭の1件のみを抽出・正規化して調査クエリ（`query` / `target_url`）に設定し、意図しない後続URLの調査や混乱を防止します。
- **多重防御とモック完結設計**:
  - `_get_img2chan_helper` によるドメインヘルパー呼び出しと正規表現抽出のフォールバック機構を備え、他サブプロジェクトの作業状況に依存せず単体完結で安全に機能します。

## 返信送信モードと削除済みメッセージ破棄 (`reply_core.py`)
- **削除済みメッセージへの返信破棄 (`_reply_to_message` / `_send_reply`)**:
  - 返信対象メッセージがユーザーにより削除済み（`discord.NotFound` または 50035 `Unknown message`）の場合、メンションでない返信（`is_direct=False`）はチャンネル通常投稿へ切り替えず安全に破棄（`ReplyTargetDeletedError` 送出・`_send_reply` は `return None`）します。本人が取り消した発言について公の場で言及するプライバシー侵害や会話の破綻を防止します。
  - メンション返信（`is_direct=True`）の場合は、ユーザーがBot宛てに明確に呼びかけた応答性を確保するため、従来どおりチャンネル通常投稿へ切り替えます。
