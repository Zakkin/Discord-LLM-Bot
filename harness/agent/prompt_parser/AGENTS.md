# 🤖 Discord AI Bot 開発・運用ガイドライン (AGENTS.md)

本プロジェクト（`discord-ai-bot`）で作業を行うすべてのAIエージェントおよび開発者は、以下の**設計前提**、**基本原則**、および**禁止事項**を必ず参照・遵守してください。

詳細な設計原則は [.agents/rules/principles.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/.agents/rules/principles.md)、具体的な禁止事項・バグ再発防止ナレッジは [.agents/rules/ban_list.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/.agents/rules/ban_list.md) を確認してください。

---

## 📋 これまでの重要不具合修正とアーキテクチャ進化の要約 (Major Milestones & Fixes)

本Botが実運用の中で直面し、解決してきた重大な課題とアーキテクチャの進化の軌跡です。すべての修正は「再発防止」として禁止事項（全131項目）および各原則に結実しています。


1. **推論モデル（LLM）の暴走防止と出力サニタイズ（項目15, 20, 21, 23, 24, 28, 32, 34, 35, 41, 47, 48, 52, 65, 74, 77, 86, 87）**:
   - 小型推論モデル（Qwen系）導入に伴う思考タグ（`<think>`）の漏洩、思考ループによるトークン枯渇（`done_reason=length` による文末途切れ）、思考抑止サフィックス（`CHAT_REPLY_OUTPUT_SUFFIX`）、未完思考からの安全なセリフ救出（サルベージ）、英語検証メモ（`(Matches exactly)` 等）の混入防止を整備。
2. **会話文脈の保護と話題乗っ取り防止（項目18, 40, 45, 49, 51, 58, 61, 62）**:
   - 複数人雑談が並行するDiscord特有の課題として、返信ツリー親メッセージ（`latest_ref`）を最優先文脈として保護。並行する他人の話題から目的語が混入する「目的語ハイジャック」を遮断し、ユーザー訂正に対する即時自己修正パイプラインを確立。
3. **自然な対話とオウム返しガードの調和（項目44, 57, 63, 64, 66, 72）**:
   - 機械的なオウム返し検知が、短文ボケへのツッコミ（「〜か…！」）、共感受容、設定解説へのリアクション、初対面挨拶（「初めまして、〇〇さん！」）を誤爆してフォールバックへ転落する問題を順次解消。丸写し（0.85以上）のみを確実に検知し、自然な対話プロトコルを保護。
4. **リトライパイプラインの完全性保証とコピペ遮断（項目48, 63, 64, 66）**:
   - リトライ時にプロンプトをスリム化しつつ `対象メッセージ` を死守。プロンプト内に見えている直前AI発言をそのまま丸コピペ出力してしまう問題を、直前発言の除去とリトライ結果再検証パイプライン（多重防御）で根本解決。
5. **不明点調査の3段階フォールバックと自然会話化（項目42, 61, 62）**:
   - 「①記憶DB検索 ➜ ②Web自然調査 ➜ ③不知表明」の3段階を策定。Web検索結果を硬いコミュニティノートや検証レポート形式で出力することを固く禁じ、1〜2文の等身大のネット評判として対話に取り込む方式へ刷新。
6. **第三者言及・関係値と言い訳返答の根絶（項目43, 56, 67, 68）**:
   - サーバーメンバーについての質問に対し、断片的な一時的困惑メモ（`thought`）をセリフ化して「名前を聞かれても意図が掴めない」と言い訳する問題を根絶。関係性パラメータから具体的キャラクター人物像を自動言語化して注入し、対話相手と話題の主を厳密分離。
7. **コンフィグ駆動設計と特定人格ハードコードの完全根絶（項目60, 69）**:
   - 共通コード層（`ollama_bot/`, `lib/`）に特定キャラクター名（コイト、シャンクス等）や特定の口癖・台詞・フォールバックをハードコードすることを全面禁止し、すべて `config_*.py` 駆動へと移行。
8. **感情・関係値の永続化と副次効果の完全保全（項目1, 5, 6, 8, 16, 22, 72）**:
   - アトミック書き込み（一時ファイル `fsync` ➜ `replace`）とオフライン時間減衰、Discord Presence / ニックネーム同期 / リアクション付与の完全保全。直前の警戒・怒りに引きずられて新規の友好的挨拶にBADリアクションを誤爆付与する現象を多重防御で防止。
9. **堅牢なDiscord API・プロセス管理と静的型安全性（項目7, 9, 10, 11, 12, 36, 37, 70, 71）**:
   - 単一プロセス・独立Bot運用の徹底、Message Context Menu上限（5個）、スラッシュコマンドのスコープクリーンアップと型整合（`User | Member`）、ローカルインポート先行参照（`UnboundLocalError`）根絶、Python 3.11+ 型付けと `mypy` 0エラーの厳格運用。
10. **サブチャンネル応答確率（サイコロ方式）の保全と型安全なチャンネル判定（項目73）**:
    - `OTHER_CHANNEL_RANDOM_MIN` / `OTHER_CHANNEL_RANDOM_MAX` を「未返信メッセージ蓄積カウンター（N件溜まるまで完全沈黙）」に勝手に変更し、メンション返答でカウンターがリセットされて永久に反応しなくなる不具合を解消。メッセージ1通ごとに独立確率抽選（サイコロ方式）を行う設計を死守。`OTHER_CHANNEL_IDS` 照合も `cfg_int_set` で型安全化。
11. **プロンプト話者名二重付与の排除と話者名付きエコー行サニタイズ（項目74）**:
    - チャンネル雑談プロンプトで `発言者:` と `対象メッセージ:` に話者名が二重付与され、LLMが台本形式と誤認して先頭に相手の発言をエコー出力する問題を是正。さらに `_strip_user_echo_prefix` に話者名プレフィックス（`re.match(r"^([^:\n]{1,40})[:：]\s*(.*)$")`）付きエコー行の検出・除去を追加し、多重防御を確立。
12. **ファクトチェック・外部調査の目的語ハイジャック遮断と基底設定継承（項目75）**:
    - ContextMenuやWeb調査において、対象発言が自立しているにもかかわらずプロンプトで直前雑談から主語を補うよう指示し、無関係な単語（王たん、新曲等）を取り込んで歌詞サイト等をヒットさせる「目的語ハイジャック」を根絶。返信ツリー親メッセージ最優先、自立文の直前雑談遮断、クエリ検証ガード（`_is_special_action_query_hijacked`）の多重防御を配備。さらに `config_base.py` の共通設定（`OTHER_CHANNEL_RANDOM_MIN/MAX` 等）が派生コンフィグの重複代入で無効化されるアンチパターンを解消。
13. **個別Botコンフィグの起動引数解決と個別設定値オーバーライドの完全性保証（項目76）**:
    - `restart-ai.sh` においてコマンドライン引数（`$1`）が無視されてアルファベット順先頭の `config_img.py`（＝`config_base.py` のデフォルト値）が強制起動され、新規作成した個別設定（`config_shanks.py` 等）が起動されない問題を解決。引数優先取得・設定名正規化（`.py` 除去・`config_` 補完）を整備。さらに共通環境変数（`OTHER_CHANNEL_RANDOM_MIN`）による個別デフォルト値の強制上書きを防ぐため、個別環境変数（`SHANKS_OTHER_CHANNEL_RANDOM_MIN`）の優先解決および直接数値代入ガイダンスを配備。
14. **理解不能・「わからない」系返答の無理な送信抑止（スキップ機能）と言語対応・メインチャンネル・直接メンション例外の保護（項目77）**:
    - サブチャンネルでの一般雑談においてLLMが「〜についてよくわからない」「意味がつかめない」「さっぱりわからない」等の理解不能・困惑・不知を出力した場合、無理に会話に返信を行わず送信をスキップ（沈黙）。
    - ただし、①「日本語しかわからない」などの言語対応案内、②`OLLAMA_CHANNEL_ID` で指定されたメインチャンネル（Primaryチャンネル）での発言、③メンションやBot宛て返信（`force_response=True`）など直接話しかけられたメッセージについては、ユーザーがBotとの明示的な対話を求めているためスキップせず例外として必ず返信。コンフィグによる制御（`SKIP_UNKNOWN_REPLY_ENABLED` 等）を整備。
15. **Bot稼働・停止制御コマンドと管理者権限制限（スタンバイ制御・多重防御）（項目78）**:
    - DiscordコマンドによるBotの起動・停止において、プロセス終了（`exit`）による復帰不能を防ぐため「アクティブ／スタンバイ状態の切り替え」として設計。設定（`BOT_ADMIN_USER_IDS`）による厳格な管理者権限制限（非管理者の `start`/`stop` 拒否）、スラッシュコマンド（`/bot` および `/bot_start/stop/status`）とテキストコマンド（`!bot`）の多重配備、スラッシュコマンド応答の秘匿表示（`ephemeral=True`）、停止時の Presence同期（DND/停止中）・メッセージ処理スキップ・自発発言抑止・全インタラクションガード（`_tree_interaction_check`）を確立。
16. **サブチャンネル応答クールダウン（不応期）とメンション保護（項目79）**:
    - 独立確率抽選（サイコロ方式）において直前返信からのクールダウンが存在せず、2〜3レス連続で返信してしまう問題を解決。チャンネルごとの通常メッセージ数カウントによる不応期（`OTHER_CHANNEL_COOLDOWN_MESSAGES` / デフォルト `RANDOM_MIN`）を確立し、上限到達時の確定返信（`RANDOM_MAX`）とその間の動的確率抽選（一様分布）を実装。メンションやBot宛て返信（`force_response=True`）はクールダウンをバイパスして即時応答を保護し、送信完了時にのみカウントを安全にリセットするハイブリッド制御を配備。
17. **discord.py イベントハンドラの親クラス非存在メソッド呼び出し防止（項目80）**:
    - `commands.Bot` のサブクラスにおいて `on_interaction` などのイベントハンドラ内で通常のOOP慣習で `await super().on_interaction(...)` を呼び出して `AttributeError` を引き起こすフレームワーク固有の落とし穴を是正。親クラスにメソッドが存在しないイベントハンドラの特性を明文化し、安全な callable ガードおよび `bot.tree.interaction_check` への集約を確立。
18. **ローカルファースト運用原則とログキー命名整合性による誤認防止（項目81）**:
    - 本番VPSへのデプロイにおいて、AIがサーバー直接書き換えを押し付けてローカルコードとの同期ギャップを生じさせるアンチパターンを根絶。「Macローカルで修正・検証完結 ➜ rsync等で本番反映」のローカルファースト原則を徹底。さらに、`_should_ignore` のログでチャンネル種別を `reason=%s` と出力し、Bot自身の自己ループ防止ログが「primaryチャンネル無視」と致命的に誤認されるログ設計バグを是正し、可観測性を向上。
19. **リリース用リポジトリ同期漏れ防止とマルチリポジトリ横断検証（項目82）**:
    - 開発ワークスペース（`discord-ai-bot`）で修正を完結させたにもかかわらず、本番リリース用リポジトリ（`discord-ai-bot-release` 等）へ反映されないまま放置され、本番環境で過去バグ（`super().on_interaction` の `AttributeError` 等）が先祖返り・再発するリリース同期ギャップを禁止。修正時は関連するすべてのリリースリポジトリ・フォルダへ同一修正を確実に適用し、`grep` や `py_compile` による横断検証を義務化。
20. **IGNORE_USER_IDS 指定ユーザーの全インタラクション遮断とエフェメラル通知・多重防御（項目83）**:
    - `IGNORE_USER_IDS` による除外が `on_message`（通常チャット）のみに限定され、スラッシュコマンドやUIボタン・モーダル等の操作が素通りできてしまう不具合を是正。`OllamaBot.on_interaction` および `bot.tree.interaction_check` の多重防御により最上流で完全遮断し、チャンネルを汚染せず本人にのみ表示される一時メッセージ（`ephemeral=True`）で利用不可を案内。オートコンプリート時のメッセージ送信例外を防止する安全例外処理を確立。
21. **質問・意見照会におけるサーバーメンバー最優先照合（User-First照合）とWeb検索バイパス（項目84）**:
    - ユーザー名や固有名称についての質問に対し、一般名詞やWeb検索に先立ってDiscordサーバーメンバー一覧・プロファイルDBと最優先で照合（User-First Resolution）。メンバー該当時はWeb検索をバイパスし、仲間としての人物像・関係性プロンプトを注入して等身大の所感で返答するパイプラインを確立。
22. **自立話題・質問における話題引きずり防止とトピック隔離（Topic Isolation）（項目85）**:
    - 短文（14文字以下）の自立した疑問・質問・挨拶が一律で話題継続（FOLLOW）に誤判定される問題を是正。自立話題における記憶検索クエリへの直前AI発言の強制合成を遮断し、プロンプトから直前AI発言・会話履歴・チャンネル要約を物理的に除外（トピック隔離）することで、小型推論モデルの話題引きずりを根本解決。
23. **外部調査プロンプト対話相手欠落・Bot自己メンション出力・文脈言及メンバー誤爆の禁止（項目86）**:
    - 外部Web調査返答のプロンプトにおいて対話相手情報（`【現在の対話相手】`）が欠落し、LLMが唯一プロンプト内にあったBot自身のメンションIDを話しかけてきたユーザー名と誤認して「@<bot自身>が調べたいのは〜ですか？」と自己メンションを出力する不具合を解決。プロンプトへの対話相手情報の確実な注入と、Bot自身へのメンションを相手呼称（`{name}さん`）に自動置換・除去するサニタイズ（`strip_bot_self_mentions`）および送信直前の多重防御を確立。
    - さらに、最新発言にメンバー名がないにもかかわらず直前会話履歴から無条件でメンバー（ブルードラシャン等）を拾ってプロンプトに誤爆注入する事象を遮断（照応代名詞がある場合のみ文脈参照）。小型推論モデルの感情理由（`reason`）への中国語・簡体字混入（「会继续」等）の検知強化とプロンプト厳格化を配備。
24. **長期記憶抽出における感情理由・別文脈からの事実捏造（ハルシネーション）の防止（項目87）**:
    - 直前の別ユーザーとの会話等によってBot内部に残存していた感情理由（`self.emotion_state.reason`）が長期記憶抽出プロンプトに `[感情の理由]` として注入され、推論モデルが今回の対話相手との出来事と誤認して「顔認証の危険性を共有し約束を交わした」などの架空事実（fact/preference）を捏造保存するバグを解決。
    - 抽出プロンプトから `[感情の理由]` を完全撤廃し、事実の捏造・架空エピソード保存の厳禁指示を明記。さらに、感情理由からの引き写し（`_is_hallucinated_emotion_reason_memory`）および会話ソースに根拠のない主要キーワードを含む記憶（`_is_unsupported_hallucinated_memory`）を事前遮断する保存前多重防御ガードを確立。
25. **ユーザー教示文におけるWeb検索誤爆防止・話題転換（BREAK）時長期記憶想起保全・無関係記憶全選択ガード（項目88）**:
    - ユーザーが「ワスとは、〜キャラクターの名称です」「玉ちゃんは〜です。覚えてください」のように知識・設定を教えている文面において、文中の「とは」や年号・日付に反応してWeb検索（Topic Investigation）が質問と誤認されて誤爆する問題を遮断（`looks_like_user_teaching_or_definition` による教示文判定と `needs_research` 抑止）。
    - 直前会話と話題が切り替わった話題転換（`flow == "BREAK"`）かつWeb調査の際、記憶検索を通らない専用関数ではなく `_generate_break_reply(..., skip_model=needs_research)` を統一経由させ、MemoryStore からの長期記憶（ワスなど）の想起を死守。
    - 小型推論モデル（Qwen 3B等）が教示文に対して無関係な過去記憶（ワス先輩等）を無差別に全選択（`[0, 1, 2]`）して混入させるバグに対し、教示キーワードと一致しない過去記憶を事後除外するガード（`_post_filter_selected_memories`）を確立。
26. **サブチャンネル自発返信確率（サイコロ方式）のクールダウンすり替え防止と生成待ち連鎖返信遮断（項目89）**:
    - `OTHER_CHANNEL_COOLDOWN_MESSAGES` / `MAX_INTERVAL_MESSAGES` が未指定（デフォルト `0` / `-1`）の際に `OTHER_CHANNEL_RANDOM_MIN / MAX` を代入して最低4通完全沈黙のカウンター方式にすり替えていたバグを解消。返信直後でも1通ごとに独立確率サイコロ抽選（`_random_one_in_range`）を行う本来の設計を死守。
    - さらに、LLM応答生成（30〜40秒）の待機中に届いた後続メッセージが連続当選してキューに溜まる問題を、自発返信エンキュー時の即時カウントリセットと同一チャンネル重複エンキュー抑止（`_active_normal_reply_cids`）の多重防御で完全遮断。
27. **感情理由のプロンプト隔離・指数表記Web検索誤爆防止・対話相手誤認呼びかけサニタイズ（項目90）**:
    - 直前ユーザーからの発言で生成された主観的感情理由（`self.emotion_state.reason` 内の「命令されて」等の言及やセリフ調テキスト）が別ユーザーへの返信プロンプトに漏洩し、LLMが「目の前の丁寧な相手から命令された」と誤認して言い返すバグを根本解決。別ユーザー返信時の理由隔離と同一ユーザー時セリフサニタイズ（`_sanitize_emotion_reason_for_prompt`）を配備。
    - 科学的指数表記（`12e23` 等）を `extract_explicit_research_term` が `e23` の部分一致で誤抽出し、高速道路番号（E23 東名阪自動車道）としてWeb検索誤爆する問題を遮断（単語境界ガードおよび `looks_like_scientific_notation_or_math` によるWeb検索バイパスとLLM推論直接委譲）。
    - 複数人会話において直前文脈の他者名（シャンクス等）で目の前の相手に呼びかけてしまう話者混同に対し、プロンプトでの他者名呼びかけ厳禁指示と出力サニタイズ（`sanitize_partner_misaddressing`）の多重防御を確立。
28. **呼称・敬称・口調ツッコミの検知保護と自然な対話プロトコル（項目91）**:
    - ユーザーからの「え呼び捨て？」「タメ口？」「さん付けして」などの直前Bot発言の呼称・敬称・口調に対するツッコミに対し、Bot発言本文にその単語自体（「呼び捨て」等）が含まれていなくても自発言ツッコミ（`is_self_tsukkomi`）として確実に検知。
    - 事前分析（Middle-LLM）の `target_is_ai: false` 誤判定をヒューリスティックおよび事後補正で `target_is_ai: True` へ保護し、主客不明瞭な指示（`avoid: ユーザーの呼び捨てを否定せず...`）をサニタイズ（「言い訳や自己弁護を避け、素直にリアクションする」）。
    - 複数人プロンプトでの呼び捨て強制指示を柔軟化（「『〇〇』さん（またはキャラクターの呼称）」）し、プロンプト指示で「呼んでるのに呼び捨てって言わないで」のような不自然な自己弁護・理不尽な反論を厳禁して自然な照れ・親愛リアクションを確立。
29. **サーバーメンバー照合（User-First）境界保護・返信（Reply）推薦文脈保護・引用文メタ復唱防止（項目92）**:
    - 「ワンパースアニメ」に含まれる「パー」などの短いカタカナ名が単語境界なしで部分一致し、サーバーメンバー言及と誤認してWeb検索や一般名詞解釈をバイパスする誤爆を遮断（カタカナ・漢字・英数字境界ガード `_is_term_matched_with_boundary`）。
    - Botの自発発言に対する返信「ワンパースアニメもおすすめ」に対し、「おすすめ」の語に反応して自立問い合わせ（BREAK）と誤判定され、「過去の話題持ち出し禁止」が注入されて会話が断絶される問題を是正（推薦表現除外および返信時の FOLLOW 最優先化）。
    - 対象メッセージ内の返信引用文（`（あなたの「...」への返信）`）を相手発言と誤認して「〜のことか！」「〜だったんですよね」とメタ復唱・釈明するエコーを、プロンプトでのプレフィックス分離・メタ復唱厳禁指示および出力サニタイズ（`_strip_user_echo_prefix`）の多重防御で根絶。
30. **定期内省（Reflection）における第三者名呼称誤認防止・保存前多重防御・背景タスク警告解消（項目93）**:
    - 特定ユーザーごとの定期内省において、記憶内の会話ログに登場した第三者名（小型人間等）を対話相手本人の好む呼び名と誤認して「相手を『小型人間さん』と呼ぶ」などの誤った信念・文化を学習・保存してしまう問題を是正。
    - プロンプトに対象ユーザーの表示名を明示（`対象ユーザーは『{target_user_name}』さんです`）し、第三者との混同および第三者名の呼称採用を厳禁する注記を配備。
    - 保存前バリデーションガード（`is_reflection_belief_valid`）により、呼称ルールにおける単発（`evidence_count < 2`）の却下および対象ユーザー名と乖離した第三者名呼称を自動破棄する多重防御を確立。
    - さらに、`_run_cancellable_background_task` において先行LLMタスク実行中の重複呼び出し時にコルーチンを明示的に `coro.close()` して破棄し、Pythonの `RuntimeWarning: coroutine was never awaited` を根本解消。
31. **ティア表パーティゲームにおける選択肢（options）の数字のみ出力抑止と多重バリデーション（項目94）**:
    - お題生成（`generate_topic_with_ai`）において、プロンプト内に出力形式の完全な具体例（お題と5つの具体的単語）および数字のみ出力の厳禁指示を明記し、JSON Schema にも `description` を配備。
    - ゲームデータモデル（`TopicData.is_valid`）において数字のみ、英字1文字、プレースホルダー、記号のみを厳格に弾くバリデーションガードを導入し、無効出力時の注意喚起付きリトライループ（最大2回）により、選択肢が「1」「2」等の数字のみになる不具合を根絶。
32. **ティア表パーティゲームにおけるAIデータベース参照お題生成と確率抽選（項目95）**:
    - お題生成（`generate_topic_with_ai`）において、AIが蓄積したデータベース（`memories`, `user_interests`, `memory_episodes`）からランダムに記憶・話題・興味をサンプリングしてプロンプトに着想元として注入し、サーバーの会話やユーザーの好みに即した白熱するお題と5つの選択肢を生成する機能を配備。
33. **ティア表パーティゲームにおける特定事例特化の排除と普遍的品質保証アーキテクチャ（項目96）**:
    - 特定のお題（動物・カモ等）や特定バグ（タヒョウ、ハロ等）に特化したモグラ叩きパッチを根絶。
    - ゲームの本質である「個人の好み・こだわり・価値観によるS〜Dの格付け（主観的評価）」を普遍的生成原則として定義し、客観的知識クイズ・分類当て・生息地当て等を普遍的正規表現（`_OBJECTIVE_FACT_TOPIC_RE`）で遮断。
    - 日本語文構造（ひらがな存在）、CJK統合漢字中の非日本語簡体字（cp932エンコード検証）排除、カタカナ音韻規則（先頭促音・長音・拗音等の排除）、極端な文字数異常（1文字ひらがな/カタカナや18文字以上の長文）、お題主題名詞との自己包含（同語反復）の多重防御をモデル層（`TopicData.is_valid`）に配備。
    - プロンプトから特定バグ語彙を完全撤廃し、多様なジャンル（グルメ、ライフスタイル、もしも、エンタメ）の高品質な具体例（Few-shot）を提示。無効出力時は注意喚起付きリトライを経て、精査済みプリセットお題（50種以上）へ確実に自動フォールバックする安全網を確立。
34. **特定ユーザーのコマンド実行制限（COMMAND_IGNORE_USER_IDS）における通常会話保全と多重防御（項目97）**:
    - 通常のAI会話・チャットは許可したまま、コマンド機能（スラッシュコマンド、UIボタン・モーダル、プレフィックスコマンド `!`、管理者コマンド等）のみを特定ユーザーに禁止する独立した制限パイプライン（`COMMAND_IGNORE_USER_IDS`）を新設。
    - `OllamaBot.on_interaction`（最上流）および `bot.tree.interaction_check` で多重防御し、対象者のみに表示されるエフェメラル通知（「❌ コマンド機能のご利用は制限されています。」）で案内。
    - プレフィックスコマンドはグローバルチェック（`@bot.check`）で本体実行を遮断しつつ、AI返信キューへの誤爆流入を抑止。一方で通常チャット・メンション会話は一切遮断せず、自然な対話を完全保全。
35. **ハーネス設計移行に伴うデプロイ同期・循環インポート完全遮断・非推奨警告抑制（項目98）**:
    - 新規アーキテクチャ層（`harness/`）の導入に伴い、`customize_for_github.py` のデプロイ対象（`allowed_items`）への追加漏れによる本番環境での `ModuleNotFoundError: No module named 'harness'` を根絶。
    - 実行形態（`python -m ollama_bot.main` や直接実行）に依存せずプロジェクトルートを確実に認識できるよう、`sys.path.insert(0, ...)` による多重動的解決ガードを配備。
    - `harness/` と既存 `ollama_bot/common/` 間のモジュールトップレベル相互インポートを排除し、ヘルパーインポートの関数内遅延解決により部分初期化循環参照（`partially initialized module`）を完全遮断。
    - Python 3.16+ 廃止予定の `uvloop.set_event_loop_policy` 警告を `warnings.catch_warnings()` で安全に抑制。
36. **感情値説明文および返答温度感ガイドラインの文法崩壊防止と自然言語化（項目99）**:
    - 感情ガイドライン（`build_reply_emotion_guidance`）において複数感情混在時に動詞句（「かなり強く明るさがにじむ」「はっきり先を気にする」）を「かつ」で生結合して「の温度感を自然に反映」を付加し、「〜にじむ、かつ〜気にするの温度感を自然に反映。」のような文法崩壊が発生する問題を解消。
    - 感情名詞（`EMOTION_NOUN_MAP`）・温度感名詞（`EMOTION_TONE_NOUN_MAP`）・単一感情トーン（`EMOTION_SINGLE_TONE_MAP`）を整備し、単一時は「{強度}{感情名詞}が前面に出ています。」、複数混在時は「{主感情}を軸にしつつ、{副感情}も混ざっています。」「{主トーン}と{副トーン}が合わさった温度感を自然に反映。」という極めて自然で滑らかな日本語表現へ刷新。
37. **ハーネス設計に基づくプロンプトマネージャー＆パーサーの完全集約（項目100）**:
    - AIエージェント・ハーネス設計（1-1. プロンプトマネージャー＆パーサー）に基づき、これまで分散していたシステムプロンプト動的生成ロジック（`build_chat_reply_system_prompt`, `build_fact_check_system_prompt`, `build_reply_investigate_system_prompt`, `build_topic_system_prompt`, `build_current_datetime_context` 等）および関連プロンプト定義群をすべて `harness/agent/prompt_parser/prompt_manager/`（`system_prompts.py`, `analysis_prompts.py`, `retry_prompts.py`）へ完全移行・集約。
    - `PromptManager` クラスにシステムプロンプト動的生成メソッド（`build_system_prompt`, `build_fact_check_system_prompt`, `build_datetime_context` 等）を集約し、すべてのプロンプト生成・分析・パース処理において接頭辞 `[prompt-manager]` を付与した統一ログ出力を確立。
    - 旧モジュール（`ollama_chat_texts.py` や `text/*.py`）は後方互換アグリゲーターへ改修し、既存呼び出しコード・全テスト（180件全パス）を破壊せず安全に移行。ディレクトリ名・パッケージ名を Python 公式パッケージ命名（`prompt_parser`）へ一本化し、50KBルールを完全遵守。
38. **プロンプト受付・事前知識収集・記憶検索（RAG）・動的結合パイプラインのアーキテクチャ確立（項目101）**:
    - ユーザーからのプロンプト受付を起点として、①入力正規化とスレッド特定、②短期記憶（直近会話履歴・返信ツリー親文脈）の抽出、③長期記憶/RAG（BGE-M3ベクトル検索＋MemoryStore）による関連記憶想起、④事前知識・コンテキスト事前解決（サーバーメンバーUser-First解決・関係値言語化）、⑤事前分析エージェント（Middle-LLM / Analyzer）による意図判定・メモリ関連性選別（無関係記憶排除）・内省・自己整合性監査、⑥システムプロンプト動的生成、⑦トークン予算管理とプロンプト結合（Budget & Builder）、⑧LLM推論実行とOutputParser（サルベージ、制御行除去、エコー除去、品質・安全性検証）による多重防御に至るエンドツーエンド実行パイプラインの設計・仕様を確立。
39. **高憎しみ・強い反発時の会話拒絶・放棄（「関係ない」等）の禁止とツッコミ・反論誘導（項目102）**:
    - 関係性パラメータが憎しみMAXや強い警戒になった際、アクションポリシー（`scoring_mixin.py`）や関係性プロンプト（`relationships_logic.py`）の「完全拒絶・冷徹」の指示に引きずられ、LLMが「関係ないですよ…！」と一方的に対話をシャットアウト（会話拒絶・放棄）してしまう問題を解消。
    - アクションポリシーを「厳戒態勢・反発（強い警戒と不満を抱き、からかいや無茶ぶりに厳しくクギを刺す・反論する）」へ刷新し、ブラックジョークや過激なボケに対しても会話を放棄せず、キャラクターらしく怯え・呆れつつ「人間ですよ！？」「通報されちゃいますよ…！」と全力でツッコミ・反論を返すプロトコルを確立。
40. **長期記憶抽出における無限ループ（Mode Collapse）・JSONトークン枯渇破損の防止とテストスタブ汚染の根絶（項目103）**:
    - 長期記憶抽出（`extract_and_store_memory`）において、件数上限・重複禁止指示の欠落、JSON Schema の `maxItems` 未指定、および低温設定（`temperature=0.1`）下での反復ペナルティ欠落により、同一記憶アイテムを20回以上連続して出力し続けて最大トークンに達し、`done_reason=length` でJSONが末尾破損して `OllamaJSONTruncatedError` で記憶保存が失敗するバグを解消。
    - スキーマへの `maxItems: 4` 配備、プロンプトでの「最大3件まで」「重複厳禁」指示の明記、および `repeat_penalty=1.15` の付与による物理的ループ遮断の多重防御を確立。さらに、`tests/test_memory_logic.py` 内のグローバルスタブ（`stub_ollama_helpers`）による他テストファイルへのモック汚染（`AttributeError`）を完全撤去。
41. **サーバーメンバーUser-First照合のDB単独依存・未チャットメンバー見落としおよび話題転換（BREAK）時想起記憶脱落・不知シラ切り表明の禁止（項目104）**:
    - サーバーメンバーについての質問・意見照会において、照合元がSQLiteの `user_profiles` テーブル（過去発言DB）のみに限定されていたため未チャットメンバー（ブルードラシャン等）が見落とされる問題を、Discordの `guild.members` とのマージ補完（`merge_guild_and_db_profiles`）により根本解決。
    - さらに、ステップ1で記憶DBから想起したトピック記憶が話題転換（`flow == "BREAK"`）時のトピック隔離（項目85）でプロンプトから脱落する問題を、`recalled_topic_memories` によるブレイクプロンプト参考情報への確実なマージで解消。Middle-LLMの `avoid` による事実説明回避の自動サニタイズ、プロンプトでの不知表明厳禁指示、および送信直前サニタイズ（`name_unknown_pattern`）の多重防御を確立。
42. **ハーネス設計に基づくツールレジストリ＆Action実行エンジンの集約・独立化（項目105）**:
    - AIエージェント・ハーネス設計（1-4. ツールレジストリ / フェーズ3. ツール選択と実行【Action】）に基づき、ツール定義・パラメータカタログ管理、OpenAI/Ollama互換 Function Calling スキーマ動的生成、同期・非同期ハンドラの透過的ディスパッチ、タイムアウト制御、引数バリデーション、および多重防御例外ハンドリングを `harness/agent/tool_registry/`（`registry.py`, `executor.py`, `types.py`, `decorators.py`）へ完全集約。
    - 外部プロトコル・ビルトイン連携アダプター（`WebSearchToolAdapter`, `WeatherToolAdapter`, `MCPToolAdapter`）を配備し、MCP（Model Context Protocol）動的インポートや既存リサーチ機能との安全な統合を確立。
43. **記憶DB・事前分析におけるメタ記憶（「意味不明」「質問した」）による実態記憶（エピソード）選外脱落の禁止、エピソード記憶からのメンバー解決（Entity Resolution）欠落の禁止、および包括的不知・シラ切りサニタイズ（項目106）**:
    - サーバーメンバーについての質問・意見照会（「〇〇についてどう思うか教えろ」等）において、Discord.py のキャッシュ仕様（Members Intent/未チャンク）により `guild.members` に不在かつプロファイルDB未登録の相手に対し、User-First解決が空振りする問題を是正。ステップ1（記憶DB検索）で取得された記憶群から、対象が人間・メンバーとして振る舞っているエピソード記憶（「〇〇が〜を持ってきた」「ユーザー（〇〇）」等）を検知した場合、自動的にサーバーメンバー・仲間（`matched_subject_member`）として解決し、プロンプトに【話題の人物】ブロックを確実に注入（Entity Resolution）。
    - さらに、記憶DB検索（`check_memory_for_topic`）において探索上限を広げ（`limit=10`）、直前の困惑メモ（「直近会話では意味不明」「AIに意見を求めている」「質問した」等）のようなメタ記憶よりも、具体的な出来事・行動・感情記憶（実態記憶）を最優先で選定して上位に採用。
    - 事前分析（Middle-LLM）のプロンプト（`analysis_prompts.py`）にエピソード記憶優先指示を明記し、選定後ガード（`analyzer.py`）において選定された記憶がすべてメタ記憶である場合に候補メモリ内の実態記憶（鈴カステラ等）へ自動昇格・置換する多重防御を配備。
    - また、小型推論モデルが出力した「そんな用語全然知らない」「って何ですかそれ」「変な名前出して困らせる」等の拒否・不知表現を包括的に検知し、自然な驚きリアクション（「急にその名前を聞かれてびっくりしちゃったんですけど…！」）に置換する出力サニタイズ（`sanitize_unknown_denial_for_known_subject`）を配備。
44. **同一ギルド内マルチBot稼働における他Bot発言（author.bot=True）・担当外チャンネル雑談・他Bot返信の最上流遮断（Early Drop）とINFOログ混信防止（項目107）**:
    - `config_img.py` と `config_test-server.py` のように複数Botを同一ギルドで並行稼働させた際、Discord Gateway の仕様により全Botにギルド内の全メッセージ（他Botの発言や別チャンネルの会話）が同報配信される。
    - 従来の `on_message` では判定前に最上流で無条件に `log.info("TRACE on_message: entered ... content=%r")` を出力していたため、他Botの発言（「はなまるうどんがいい」等）が自Botのログファイルへ混入する問題を根本解決。
    - ハンドラ最上流で `author.bot == True` を判定して `log.debug` で即座に破棄（早期リターン）し、担当外チャンネル（`kind == "none"`）の通常雑談および他Bot宛て返信（「まどマギのフィルムだっていってるだろ？」等）も、`force_response` 計算前に最上流で `log.debug` で早期スキップ。自Botが関与するチャンネル・メッセージのみを `log.info` の対象としてログ分離を完全保証。
45. **気象情報処理の国内気象MCP（JMA・ウェザーニュース）移行と旧Open-Meteo依存・旧weather_helpers直接参照の完全撤廃（項目108）**:
    - 従来使用していた `ollama_bot/common/weather_helpers.py` 内の Open-Meteo API 依存（`geocode_place`, `_PRESET_COORDINATES`, WMOコード変換等）を完全撤廃し、気象庁（JMA）およびウェザーニュースの国内気象情報 MCP（`harness/agent/tool_registry/mcp/weather/`）をバックエンドとするアーキテクチャへ全面移行。
    - `JMAForecastResult` に対象日（今日/明日/明後日）の要約生成メソッド（`get_forecast_for_when`, `format_summary`）を追加し、英語地名（"Tokyo", "Osaka", "Hiroshima" 等）および大文字小文字に対応したエリアコード解決（`resolve_jma_area_code`）を整備。
    - `WeatherToolAdapter.execute_weather` から旧 `weather_helpers` への直接インポートを排除し、MCP の `JapanWeatherService` を直接呼び出すように刷新。
    - `weather_helpers.py` は MCP の `JapanWeatherService` を呼び出す薄い後方互換レイヤーとしてリファクタリングし、Discord 対話特有のクエリ判定・地名トリミング・文脈からの事実抽出・フォローアップ応答生成を気象庁フォーマットにも適合させて完全保全。
46. **メッセージ生成中におけるDiscord入力中インジケータ（typing）の長時間表示放置の禁止、完成後即時出力設計、およびコンフィグ駆動制御（項目109）**:
    - Botがメッセージを生成する際、`_call_with_typing` がLLM推論全体を `channel.typing()` でラップしていたため、推論・思考・リトライ中（十数秒〜数十秒）ずっとDiscord上で「〇〇が入力中、、、」と表示され続けてユーザーを過度に待たせる状態を解消。
    - `config_base.py` に `DISCORD_TYPING_INDICATOR_ENABLED`（デフォルト `False`）を新設。無効時は文章生成中の入力中表示をバイパスし、最終文章が完全に完成した段階で即座にメッセージを出力（送信）する設計へ刷新。自発発言（`proactive.py`）でも同様に設定を適用。
47. **ハーネス設計に基づく5段階エージェントフロー（Observation・テストハーネス・オーケストレーター）の完全導入と旧コード・直接呼び出しフォールバックの完全撤廃（項目110）**:
    - ユーザープロンプトから最終返答生成に至るAIエージェントの内部処理を、ハーネス設計に基づく明確な5段階フロー（1. Context ➔ 2. Thought ➔ 3. Action ➔ 4. Observation ➔ 5. Final Response）へ刷新。
    - フェーズ4基盤として `harness/agent/observation/`（`ObservationProcessor`, `ObservationEvaluator`, `LoopDetector`, `ObservationSynthesizer`）を新設し、ツール実行結果の客観的評価・ループ検知・再思考フィードバックを確立。
    - エージェント監視・保護・検証基盤として `harness/agent/test_harness/`（2-1 Sandbox, 2-2 Telemetry, 2-3 Evaluator: 機械的アサーション・LLM-as-a-Judge・環境検証）を整備。
    - 全体を統括する `harness/agent/orchestrator/`（`AgentOrchestrator`）を実装し、Botの返信生成（`_call_reply_model_or_fallback`）を100%オーケストレーター経由へ一本化。旧来の直叩き二重呼び出しフォールバックを完全撤廃。
    - さらに、旧フォワーダー（`ollama_bot/common/reply_safety/*.py` および `ollama_reply_safety.py`）を物理削除し、全回帰テストの import を `harness.agent.prompt_parser.parser` へ一括統合。
48. **ハーネス設計に基づく各フェーズ（Context/Thought/Action/Observation）の体系的フォルダ再編とBot特化Observation機能群の確立（項目111）**:
    - AIエージェント・ハーネス設計を深化させ、`ollama_bot/` 配下に残っていた各機能をハーネスの4つのサブディレクトリへ体系的に移行・集約：
      - ① コンテキスト構築（知覚・記憶）: `harness/agent/prompt_parser/prompt_manager/`（`chat_prompt_logic.py`, `context_helpers.py`, `history_helpers.py`, `working_memory.py`, `user_profile_logic.py`, `user_interest_logic.py`, `prospective_memory.py`）
      - ② 推論と計画立案（Thought）: `harness/agent/prompt_parser/parser/`（`reply_helpers.py`, `habit_policy.py`, `reply_guards.py`）
      - ③ ツール選択と実行（Action）: `harness/agent/tool_registry/`（`web_research/` 一式, `weather_helpers.py`, `game_pool_manager.py`, `umigame_helpers.py`, `singing_helpers.py`, MCP連携アダプター）
      - ④ 結果の観察と評価（Observation）: `harness/agent/prompt_parser/observation/`（汎用観察基盤に加えて、Bot特有の要件を満たす専用エバリュエーターを新設）
    - **Bot特化Observationコンポーネントの新設**:
      - `WebObservationEvaluator`: Web検索・ブラウジング結果の精査、目的語ハイジャック検知（項目75）、ノイズドメイン除外、情報十分性判定（`GOAL_ACHIEVED` / `NEED_MORE_INFO`）。
      - `FactCheckEvaluator`: ファクトチェック・裏取り結果の観察、事実性判定（`TRUE` / `FALSE` / `PARTIALLY_TRUE` / `UNVERIFIABLE` / `OPINION`）および信頼度スコア評価。
      - `FeedbackObserver`: Discordリアクション（ユーザーからの環境フィードバック）の観察、好意度・親愛度の感情・関係値へのフィードバック評価。
      - `ReplyObservationEvaluator`: LLM生成応答の観察、オウム返し（閾値0.85）、思考タグ（`<think>`）漏洩、文末途切れ、不知スキップ（項目77）の妥当性評価、不合格時のフェーズ2（Thought）差し戻し用リトライ指示プロンプト自動生成。
      - `ReflectionEvaluator`: 自己内省・行動ログの観察、第三者名呼称誤認防止バリデーション（エビデンス数判定）。
    - **完全後方互換フォワーダーの完全撤廃と新ハーネスパス直接参照への一本化（項目110, 111）**:
      - 移行完了に伴い、旧パス（`ollama_bot/common/` および `ollama_bot/ollama_chat_/`）に存在していた計34個のフォワーダーファイル・ディレクトリを完全撤廃（物理削除）。
      - プロダクションコードおよびテストコードの import / mock パスを新ハーネスパス（`harness/agent/...` や `lib/...`）へ直接参照に一本化。旧パスへの逆参照を完全禁止し、全277ソースファイルで mypy 0 エラーおよび全808件テスト合格を達成。
      - `harness.agent.observation` と `harness.agent.prompt_parser.observation`、および `harness.agent.tool_registry` と `harness.agent.prompt_parser.tool_registry` 間で型定義を完全統一し、型チェッカー（mypy）における別型誤認を根本排除。
49. **マルチBot並行稼働における記憶DB（SQLite）・ゲームプール・感情/関係値・投稿履歴のコンフィグ単位自動分離（Auto-Isolation）と共有パス放置の禁止（項目112）**:
    - `config_img.py` と `config_test-server.py` 等のマルチBot並行稼働において、ログだけでなく永続化ストレージ（記憶DB `memory.db`、感情/関係値 `emotion_state_*.json`, `user_relationships_*.json`、ゲームプール `game_pool.json`、ふたば投稿履歴 `img2chan_post_history.jsonl`、プロセスロックファイル等）が単一の固定パスに放置され、全Bot間で記憶・感情・お題・投稿履歴が混同・衝突する致命的不具合を是正。
    - `lib.config_utils.resolve_isolated_path(cfg_key, default_pattern, legacy_fallback_config="config_img")` を導入し、個別コンフィグで明示指定がない場合でも起動コンフィグ名（`config_name`）に基づき自動的にパスを分離（`memory_{config_name}.db` 等）する「ゼロコンフィグ自動分離（Auto-Isolation）」を確立。
50. **ファクトチェック機能のMCPサーバー（FactCheckMCPServer/Client）完全移行と直接参照への一本化（項目113）**:
    - `ollama_bot/common/fact_check.py` に直書きされていたファクトチェック機能（URL展開、X/Twitterメタデータ抽出、Web検索、リンク要約、テキスト抽出、アクション検出、検証実行）を、Model Context Protocol（MCP）準拠の独立サーバー（`FactCheckMCPServer`）、クライアント（`FactCheckMCPClient`）、サービス層（`FactCheckService`）およびツールアダプター（`FactCheckToolAdapter`）へ全面移行（`harness/agent/tool_registry/mcp/fact_check/`）。
    - 4つの標準MCPツール（`fact_check_verify`, `fact_check_search_web`, `fact_check_extract_context`, `fact_check_detect_action`）およびJSON-RPC 2.0 / FastAPIエンドポイントを定義し、外部エージェントからの標準プロトコル呼び出しとインプロセス直接実行のハイブリッド実行を確立。
    - 移行完了に伴い、旧パス `ollama_bot/common/fact_check.py` フォワーダーを完全撤廃（物理削除）。プロダクションコードおよびテストコード（`tests/test_fact_check.py` 等）を `harness.agent.tool_registry.mcp.fact_check` への直接参照・モックに一本化し、全277ソースファイルで mypy 0 エラーを達成。
51. **感情評価コンテキストにおける内部メモ・記憶の「相手発言」誤認排除、事前分析・関係値ブロックへの対話相手名明記と第三者混同防止、および返答生成プロンプト冒頭での対話相手早期宣言（項目114）**:
    - `runtime.context_lines` 内のBot思考メモ（`System: (内心のメモ: ...)`）や記憶検索結果（`（コイトの記憶・実感: ...）`）がコロン分割で「相手の発言」として誤ラベリングされ、感情評価LLMが「相手がトラウマ記憶を突きつけて攻撃してきた」と被害妄想を起こして憎しみ+10.00・極冷淡判定に転落する致命的不具合を是正（`_is_internal_or_system_context_line` による検知と `relabel_emotion_context_line` 空文字除外の完全防御）。
    - 事前分析（Middle-LLM）プロンプトに `current_user_name`（対話相手の表示名）を明記し、話題のサーバーメンバー（ブルードラシャン等）に関する記憶と対話相手を明確に分離。
    - 関係値ブロックの見出しを `【対話相手（『{target_user_name}』さん）との関係性・感情】` とし、話題の第三者に対する感情ではない旨の注意喚起を注入して第三者混同による冷淡・拒絶を遮断。
52. **複数Bot並行稼働における他Bot宛てメッセージ素通り遮断（五重防御）による感情値・感情理由・ステータス誤更新・ログ混入の根絶、および記憶DBペルソナ名前空間の完全分離（項目115）**:
    - `config_img.py` と `config_test-server.py` のように複数Botを同一ギルドで並行稼働させた際、他Bot宛ての会話（他Botメンション、他Bot返信、他Bot名呼びかけ）が自Botのパイプラインに素通りし、自Botの感情値（scores）・感情理由（reason）・吹き出しステータス（Activity）・Bio が上書きされ、自Botログへ他Bot宛てメッセージが混入する問題を根絶。
    - `is_message_addressed_to_other_bot` を導入し、① `on_message` 最上流（INFOログ出力前）、② `_should_ignore`、③ `_process_queued_message`、④ `_run_emotion_update_for_message`、⑤ `_update_emotion_state` の五重防御で他Bot宛てメッセージを完全遮断。
    - 記憶DBのファイル分離（`memory_{config_name}.db`）に加え、ペルソナ名前空間も `cfg_persona_namespace` により自動解決（`config_test-server` ➜ `test-server`）して完全分離を死守。
    - URL投稿時のXキーワード検索（`x_search`）誤爆を修正し、URL共有時のURL閲覧（`browser_read_url`）最優先を確立。全883件テスト合格および mypy 0 エラー（311ファイル）を達成。
53. **サブチャンネル応答頻度設定における最大間隔（確定返信）・クールダウン連動欠落による長期沈黙放置の禁止、および個別コンフィグ値優先解決と抽選可観測性の保証（項目116）**:
    - 個別Botコンフィグで `OTHER_CHANNEL_RANDOM_MIN/MAX` を指定した際、`OTHER_CHANNEL_COOLDOWN_MESSAGES`（不応期）および `OTHER_CHANNEL_MAX_INTERVAL_MESSAGES`（最大間隔確定返信）の定義欠落により、無記憶性の幾何分布抽選で150〜200通以上スルーされる不具合を解消。個別設定値とクールダウン・確定返信の自動連動を確立。
    - 個別環境変数（`IMG_OTHER_CHANNEL_RANDOM_MIN`）の優先解決および直接デフォルト値代入による共通環境変数上書きを防止。TRACEログ（`min/max/chosen_n/roll/hit`）による抽選可観測性を向上。
54. **ReAct ハーネス設計への完全移行と5分類（Context/Thought/Action/Observation/Orchestrator）アーキテクチャの確立（項目117）**:
    - 本Botの内部機能を明確な ReAct 設計（Context ➜ Thought ➜ Action ➜ Observation ➜ Final Response / Orchestrator）の5分類へ体系的に移行・集約：
      - ① **Context (知覚・記憶)**: [`harness/agent/prompt_parser/prompt_manager/`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/prompt_parser/prompt_manager/) にプロンプト生成・長期記憶RAG検索・サーバーメンバー解決・関係性プロンプトビルダー（`relationship_prompts.py`）を集約。
      - ② **Thought (推論・サニタイズ)**: [`harness/agent/prompt_parser/parser/`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/prompt_parser/parser/) に思考タグ除去・JSONスキーマ検証・出力サニタイズ（プレースホルダー検知 `looks_like_placeholder_thought`、過去ログ丸写し検知 `looks_like_copied_log_appraisal`）を集約。
      - ③ **Action (ツール実行)**: [`harness/agent/tool_registry/`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/tool_registry/) を正本としてツールカタログ（`ToolRegistry`）、Web調査（`web_research/`）、国内気象MCP、ゲーム実行（`game_pool_manager.py`, `umigame_helpers.py`, `singing_helpers.py`）を一元管理。
      - ④ **Observation (結果の観察・評価)**: [`harness/agent/observation/`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/observation/) に感情観察・中国語混入遮断・架空事実捏造防止（`EmotionObservationEvaluator`）、目的語ハイジャック検知（`WebObservationEvaluator`）、リアクション観察（`FeedbackObserver`）、内省信念バリデーション（`ReflectionEvaluator`）を集約。
      - ⑤ **Orchestrator (フロー統括)**: [`harness/agent/orchestrator/`](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/harness/agent/orchestrator/) の `AgentOrchestrator` が5段階自律ReActサイクルを統括。
    - 旧パス（`prompt_parser/tool_registry/` 等）への後方互換として `sys.modules` レベルで正本モジュールを共有する互換 shim を配備し、テストのモック（`patch.object`）破壊を防止。全70ファイル・700件超テスト全パスおよび mypy 0 エラー（272ファイル）を達成。
55. **推論・計画立案における自己発言の前言撤回・未体験者ぶり自己矛盾、ロール反転、および語彙すり替えハルシネーションの禁止（項目118）**:
    - AIが直前の発言で表明したスタンス、好み、主張（例: 「魚フライ定食大好き」等）について、相手から「マジすか」「本当？」「マジで？」「知ってるの？」等の確認・驚きのリプライを受けた際、立場を逆転させて未体験者ぶったり（「食べたことない」「いつか食べてみたい」等）、相手に感想を聞き返す前言撤回・ロール反転を根絶。
    - `looks_like_ai_utterance_confirmation` ヘルパーにより、事前分析（Middle-LLM）で `target_is_ai=True`, `intent='確認'`, `reply_strategy='肯定・スタンス維持'`, `avoid='前言撤回や未体験者ぶる態度を避ける'` として保護・事後補正。
    - さらに、自発言の言い間違いを認めるツッコミ処理（`is_self_tsukkomi`）が誤爆して前言撤回に誘導される事態を遮断し、「直前の自発言への確認・驚きへの応答指示」専用ブロックを注入。
    - また、文脈上の名詞（「魚フライ」等）から勝手に連想を膨らませて別の単語（「天ぷら」等）にすり替えるハルシネーションを禁止する指示をプロンプト全体（返信ツリー、直前発言、全体ルール）に配備（多重防御）。
56. **意図解釈・調査計画立案（Thought）におけるHow-to/SNS話題混同、ルールによるモデル推論上書き、およびリプライ時新規質問のメタ解説誤爆の禁止（項目119）**:
    - ユーザーからの「Xの動画をダウンロードする方法を教えて」等の発話に対し、「X」という単語に釣られてSNS上の最新ポスト検索（`x_search`）を選択する誤った計画立案を是正。操作手順・ツールの利用方法（How-to）はWeb上の解説記事を調べる一般Web検索（`browser_search`）を選択し、SNS上の投稿そのものを知りたい場合のみ `x_search` を選択する推論指針をプロンプト（システムプロンプト・ユーザープロンプト）に明記。
    - ユーザーの長文質問をそのままクエリに流し込まず、意図に応じた検索キーワード（例: `X 動画 保存 方法`）を `search_query` として抽出・立案するプロンプト指示を強化。
    - コード側のルール判定で `x_search_flag` が立っている場合でも、モデルが `browser_search` を選択した、またはユーザー入力がHow-to（方法・やり方・ダウンロード・保存・使い方等）である場合は、ルールの `x_search_flag` による上書きを抑止してモデルの意図推論（`browser_search`）を最優先するよう計画立案ロジックを改修。
    - さらに、Bot発言へのリプライ（Reply）ツリーで行われた発言が、「直前メッセージ自体の解説・要約要求（メタ操作: `これをもっとわかりやすく解説して`等）」なのか、「新しいトピックについての質問文（主語や目的語を含んだ自立した質問: `XでGifをダウンロードする方法を教えて`等）」なのかを正しく推論・判別するガード（`is_standalone_reply_question`）を配備。自立したトピック質問である場合は直前メッセージの解説機能（`simplify`）を起動せず、通常の会話応答サイクルへルーティング。
57. **記憶マネージャーの完全ハーネス移行（harness/agent/memory-manager）と旧後方互換レイヤーの物理削除（項目120）**:
    - `ollama_bot` 配下に残存していた旧DB・記憶関連コードおよび後方互換レイヤー（`ollama_bot/common/memory_store.py`, `memory_logic.py`, `reflection_logic.py`, `ollama_bot/common/memory/` ディレクトリ全体）を完全撤廃・物理削除。
    - 全本番コード（11ファイル）およびテストコード（15ファイル以上）を直接 `harness/agent/memory-manager`（実体: `harness.agent.memory_manager`）参照へ切り替え、ゾンビインポートを根絶。
    - ハーネス側では `MemoryStore` の責務別分割（`base.py`, `core_store.py`, `interactions.py`, `profiles.py`, `learning.py`）により 50KB ルール（全ファイル 24KB 以下）を厳守。
    - 公開ファサード（`logic.py`）の動的委譲によるモック追従性の保証、`retriever.py` における MagicMock await 多重防御（`_safe_await`）、およびシステムプロンプト固定ルール文（「魚フライ」等）とのテスト用ダミー文言重複による偽陽性解消を配備。
    - 全923件テスト PASS、mypy 0 エラー（248ファイル）、およびリリース用リポジトリ（`discord-ai-bot-release`）への完全同期を達成。
58. **Agentic Loopにおけるフェーズ間メモリデータフロー（Read/Write/Save）の断絶、Observationルーターの軽微ループ警告（ALTERNATIVE_TOOL）によるFATAL_ERROR誤爆、および致命的エラー中断時LLM最終推論リークの禁止（項目121）**:
    - **フェーズ1（コンテキスト構築・Read）の動的記憶想起**: オーケストレーター（`engine.py`）の `_build_context` が同期関数で静的辞書展開に留まり、`MemoryStore`（`search_memories_hybrid` / `get_channel_summary`）から動的な長期記憶（RAG）や会話要約を読み込めない問題を解消。非同期化と動的ハイブリッド検索・要約取得パイプラインを確立。
    - **フェーズ4（結果の観察と評価・Write / Retry）の再試行保護とコンテキスト管理**: 同一ツールの反復実行など軽微なループ警告（`ALTERNATIVE_TOOL`）が `or ev.error_loop_detected` により即座に `FATAL_ERROR` に誤爆して中断していた不整合を是正。`ev.verdict == EvaluationVerdict.FATAL_ERROR` のみを安全停止とし、別アプローチ再試行（`RETRY_WITH_THOUGHT`）へのフィードバック伝播とループフラグ保持を両立。さらにターン間の `ScratchpadManager.trim_history_window` 呼び出しによりコンテキスト長肥大化を防止。
    - **フェーズ5（最終応答と制御・Prune/Save）の長期記憶定着と安全停止ガード**: タスク完了時に `_save_memory` が空スタブ（ログ出力のみ）で何も保存していなかった欠陥を解消し、Bot発話（`add_bot_utterance`）、観察確定事実（`add_memory`）、ツール実行エピソード（`add_episode`）を長期記憶へ定着保存するパイプラインを完全実装。さらに致命的エラー（`FATAL_ERROR`）による安全停止時は、LLMへの不要な最終推論および記憶保存を確実にバイパスし、安全なフォールバック応答で即座に終了する安全ガードを配備。
59. **個別Botコンフィグごとのプロフィール（About Me / Bio）同期分離と個別トークン・ギルド優先解決（項目122）**:
    - `config_img.py` と `config_test-server.py`（または `config_xxxx.py`）のように複数Botコンフィグを運用した際、一方のBotで生成された感情理由（「コイトに面白いスレッドを見つけてくれて...」等）が `_sync_emotion_bio` の `PATCH /applications/@me`（グローバルBio更新）によりもう一方のBotのプロフィール欄（About Me）に漏洩・反映されてしまう不具合を解決。
    - 原因として、各コンフィグが共通環境変数（`DISCORD_TOKEN`, `DISCORD_GUILD_ID`）に盲目的に依存し、個別環境変数（`IMG_DISCORD_TOKEN`, `TEST_SERVER_DISCORD_TOKEN` 等）を解決する仕組みが欠落していたため同一Botアカウントで並行稼働してプロフィールを上書きし合っていたこと、およびグローバルBio更新のコンフィグ単位制御が欠落していたことを特定。
    - `lib/config_utils.py` に `get_bot_config_prefix`、`resolve_bot_token`、`resolve_bot_guild_id`、`cfg_emotion_bio_enabled` を配備し、個別プレフィックス付き環境変数の最優先解決を確立。
    - `EMOTION_BIO_TARGET_CONFIGS`（Bio更新対象Botコンフィグの限定設定）を新設し、テストサーバー用Botや別Botからのグローバルプロフィール上書きを完全に遮断。さらに `_sync_emotion_bio` に実行/スキップTRACEログを配備し可観測性を向上。
    - `_sync_emotion_presence` において `GUILD_ID` 指定時にギルドが見つからない場合、全ギルドにフォールバックして他サーバーのニックネームを書き換える誤爆を遮断。全926件テスト全パス、mypy 0 エラー（248ファイル）、およびリリースリポジトリへの完全同期を達成。
60. **ファクトチェック・外部調査における検索クエリへの連想単語混入、未確認判定時の背景解説・由来捏造、および未確認結果からの架空事実（fact）長期記憶保存の禁止（項目123）**:
    - ファクトチェックや What is this 等の外部調査キーワード抽出において、モデルが勝手な連想や思いつきで別ゲーム名・別サービス名などの外部単語（「ポケモンGO」等）を混入させる問題を是正。`temperature=0.0` の明示指定、プロンプトでの外部連想単語禁止、および自立文での外部幻覚単語除外パイプライン（`sanitize_query_hallucinations`）を配備。
    - ファクトチェック回答生成プロンプトに「検索結果にない背景知識・由来・企業情報の推測・捏造厳禁」を明記し、検索結果に根拠がない場合は「確認できなかった」旨のみを落ち着いて説明し、それ以上の推測・解説を語らないよう徹底。
    - ファクトチェック結果が未確認・根拠なしと判定された話題に関して、AI自身が推測した架空の補足説明（「〇〇は△△の子会社で〜由来は…」等）が客観的事実（fact）として長期記憶に保存される脆弱性を解消。未確認話題は `fact` ではなく `conversation_theme`（score=0.50）として安全に記録し、記憶抽出プロンプトおよび事後バリデーション（`_is_unconfirmed_factcheck_fabrication`）により未確認文脈での架空由来・公式見解保存を二重遮断。
61. **マルチBot並行稼働におけるサーバー別メンバー自己紹介（Guild Member Bio）による同一トークン下での完全分離と他Bot混同防止（項目124）**:
    - `config_img.py` と `config_test-server.py`（または `config_xxxx.py`）のように同一のBotトークンを用いて複数Botを並行稼働させた際、全サーバー共通の `PATCH /applications/@me` を用いていたために最後に更新したBotの感情理由で他サーバーの自己紹介が上書きされてしまう問題を根本解決。
    - 各Botは担当サーバー（`resolve_bot_guild_id()`）を特定し、Discord API のサーバー別メンバープロファイル（`PATCH /guilds/{guild_id}/members/@me`）に対して `json={"bio": bio_text}` を送信するパイプラインを確立。
    - これにより、Botトークンを分けられない同一トークン環境であっても、**testサーバーでは【test】、本番サーバーでは【img】の感情・理由セクションのみがサーバーごとに独立して表示**され、互いのサーバーの自己紹介を上書き汚染することが完全に根絶される。
    - 担当ギルド未設定時やAPIエラー時は従来の `PATCH /applications/@me` へ安全にフォールバックする多重防御を配備。コンフィグ駆動設計（`EMOTION_BIO_ENABLED`, `EMOTION_BIO_LABEL`）を死守。全62件テスト合格、mypy 0 エラー（83ファイル）を達成。
62. **定期内省（Reflection）におけるユーザー紐付け・抽象主語「ユーザーは」の正規化および蓄積パーソナリティの返信反映（項目125）**:
    - 定期内省（Reflection）において、LLMが「ユーザーは『ワス』というキャラクターへの深い愛着と...」「ユーザーは深夜に空腹を感じた際に...」のように抽象的な「ユーザーは」を主語にして信念（belief）を出力・保存してしまい、誰に対するパーソナリティ・接し方・文化なのかが曖昧になる問題を根本解決。
    - **プロンプト指示の強化**: `build_reflection_prompt` および `DEFAULT_REFLECTION_SYSTEM_PROMPT` において、対象ユーザー名（`『{target_user_name}』さん`）およびユーザーIDを明示し、信念・文化の主語を「ユーザーは」ではなく必ず「『{target_user_name}』さんは〜」と記述するよう厳格指示を注入。
    - **事後正規化ガード（多重防御）**: 小型推論モデルが指示を逸脱して「ユーザーは」「相手は」「このユーザーは」と出力した場合でも、`normalize_reflection_belief` により対象ユーザー名（またはID）に自動置換・正規化してDBへ保存。さらに過去に「ユーザーは〜」として保存された既存DBレコードも、返信生成時の取得（`_get_core_beliefs_for_prompt`）時に動的に対象ユーザー名へ自動正規化。
    - **蓄積パーソナリティの返信時への全面反映**:
      - 通常返信（`_build_user_prompt` / `build_agent_persona_parts`）において、蓄積された信念を全体文化と対話相手特有の文化に自動分類し、`【対話相手（『{current_user_name}』さん）との共有文化・接し方のパーソナリティ方針】` としてプロンプトに強調注入。
      - 話題転換返信（`_generate_break_reply_from_memory`, `from_x`, `from_img`, `from_user_interest`）および外部調査BREAK返信（`_build_research_break_base_prompt`）において、従来欠落していた `core_beliefs`（蓄積パーソナリティ）を確実に取得・注入し、話題転換時や調査返答時でも相手との関係性・共有文化（「ワス好き」「鈴カステラ」「帽子を預けて海賊になる約束」等）が自然ににじみ出る対話パイプラインを確立。

63. **長期記憶抽出（reason/memories）におけるサーバーニックネーム・ユーザーID紐付け＆事後正規化、および返信時におけるアカウント名呼称排除とサーバーニックネーム多重防御（項目126）**:
    - **長期記憶抽出におけるサーバーニックネーム＆IDの確実な反映**:
      - `extract_and_store_memory` において、対象ユーザー情報が欠落していたため `training_candidate.reason` や `memories[].content` で「ユーザーが外部リンクを提示し、AIがその内容を確認して返答した...」のように抽象的な「ユーザー」が出力されていた問題を根本解決。
      - 呼び出し元（`_store_and_update_summary`）から `user_name` および `user_display_name`（サーバーニックネーム）を渡し、プロンプトに対象ユーザーのサーバーニックネーム（『〇〇』さん）およびIDを注入。主語としてサーバーニックネームを用いるよう厳格指示。
      - さらに、小型推論モデルが指示をすり抜けて「ユーザーが〜」「ユーザーは〜」を出力した場合でも、保存前事後正規化ガード（`normalize_memory_text`）により文頭・文中の「ユーザーが」「ユーザーは」「ユーザーの」「ユーザーにとって」「相手が」等を対象のサーバーニックネーム（『〇〇』さんが）またはユーザーIDへ自動置換・正規化する多重防御を確立。
    - **返信時におけるアカウント名呼称の完全排除とサーバーニックネーム強制**:
      - 返信時に対象ユーザーのアカウント名（username: `zakkin` 等）で呼びかけてしまう問題を根絶するため、`MessageRuntime` 構築時（`_build_runtime`）に `author_name` および `author_display_name` の両方で `display_name(message)`（サーバーニックネーム）を最優先代入。
      - さらに、出力サニタイズ（`_eff_other_names` / `sanitize_partner_misaddressing`）に相手の生アカウント名（`_raw_author_name`）を追加し、LLMがプロンプトの隙間からアカウント名を拾って出力した場合でも、送信直前にサーバーニックネームへ自動置換される多重防御を確立。

64. **サーバーニックネーム変更（改名）時における記憶・信念・関係値の見失い防止、内部ユーザーID紐付け管理、および最新サーバーニックネーム動的置換・レンダリング（項目127）**:
    - **課題の本質**:
      - ユーザーがDiscordサーバー上でニックネームを変更（例: 「でゅねいンマン」➜「超でゅねいンマン」）した際、過去にDBへ保存された長期記憶（memories）、定期内省（reflections）、関係値（user_relationships）のテキストに当時の旧ニックネームがハードコードされていたため、同一人物を見失ったり、プロンプトや `/relation` コマンドで古い名前が表示されてしまう課題を根本解決。
    - **内部ユーザーID管理と動的エイリアス解決基盤（UserIdentityResolver）の新設**:
      - `harness/agent/memory_manager/user_identity_resolver.py` を新設し、不変のユーザーIDを正本として管理。内部記録には `[uid:12345]` 識別タグを保持可能とし、`user_profiles` テーブルから過去の改名履歴（`aliases`）を取得するパイプラインを確立。
      - プレースホルダートークン方式を用いた `replace_aliases_with_current_name` により、新旧名が部分一致する改名パターンでも多重置換を起こさず、旧ニックネームを最新のサーバーニックネームへ安全に置換。
      - `render_user_facing_text` により、内部タグ `[uid:...]` の除去と旧エイリアスの最新名置換を統合した外部表示用レンダリング関数を配備。
    - **全パイプラインへの動的置換レンダリングの統合**:
      - **プロンプト注入**: `format_core_beliefs_for_prompt` および `build_agent_persona_parts` において、ユーザーIDおよびエイリアス履歴から改名後のユーザーを確実にキャッチし、最新サーバーニックネームへ動的置換してプロンプトへ注入。通常返信（`builder.py`）、全BREAK返信（`ollama_chat_reply_generation.py`）、および外部調査返信（`research_prompts.py`）へ完全適用。
      - **関係性コマンド表示**: `/relation` コマンド（`commands_mixin.py`）において、所感（`thought`）内の旧ニックネームを最新サーバーニックネームへ動的置換して embed 表示。所感生成（`scoring_mixin.py`）でも `author_display_name`（サーバーニックネーム）を最優先化。
      - **記憶・内省の正規化**: `normalize_reflection_belief` および `normalize_memory_text` にエイリアス置換を統合し、保存時および取得時の多重防御を確立。
65. **複合質問における検索クエリ単語切り詰め遮断・日常カタカナ語未知語誤爆防止・情報不足時メディアジャンル飛躍ハルシネーション抑止（項目128）**:
    - **課題の本質**:
      - 「セブンイレブンのミルク風味の鈴カステラ味を教えて」と質問した際、①「セブンイレブン」がカタカナ6文字以上として専門未知語（`_looks_like_specialized_unknown_term`）と誤判定され、②検索クエリが先頭の「セブンイレブン」のみに切り詰められて商品名（ミルク風味の鈴カステラ味）が消失し、③検索結果が会社概要のみとなり情報不足になった小型推論モデル（Qwen 3.8B系）が「カステラ」「鈴」から「カセットテープやCD（音楽作品）」へと連想を飛躍させて架空の作品・言い訳を捏造した不具合を是正。
    - **日常カタカナ語・チェーン店名ストップワードとクエリ保持**:
      - `_COMMON_KATAKANA_STOPWORDS`（セブンイレブン、ファミリーマート、カステラ、アイスクリーム等）を配備し、有名チェーンや一般的食品・日用品が専門未知語として誤検出されるのを防止。未知語フラグ（`unknown_term_flag`）も定義質問時のみに限定。
      - 単語定義質問でない限り `_choose_query(...)` を優先し、ユーザーの質問文全文（「セブンイレブンのミルク風味の鈴カステラ味を教えて」）を検索クエリとして維持するパイプラインを確立。
    - **情報不足時プロンプトガードと事後サニタイズ（多重防御）**:
      - プロンプト指示（`info_shortage_guard`）において、検索で見つからない場合に対象を勝手に音楽・CD・カセットテープ等の作品やアーティストと推測・捏造することを明示的に禁止。
      - さらに、出力サニタイズ（`sanitize_unrelated_media_hallucination`）を配備し、食品・日用品文脈で音楽メディア（カセットテープ、CD、楽曲等）への飛躍が発生した場合は送信直前に安全に除去・整形する多重防御を確立。
66. **会話習慣プロファイル（update_user_habit_profile/reason）におけるサーバーニックネーム・ユーザーID紐付け＆事後正規化（項目129）**:
    - **課題の本質**:
      - ユーザーとの会話終了時に実行される会話習慣プロファイルの更新（`update_user_habit_profile`）において、対象ユーザー情報（サーバーニックネームおよびID）がプロンプトへ伝播されず、LLMが「ユーザーの発言は具体的な商品名の問い合わせであり、冗談や皮肉が含まれていないため、ユーモア受容度や口調の好みを判断する材料が不足しています。」のように抽象的な「ユーザー」を主語にして出力し、DBテーブル `user_habit_profiles` の `source_summary` にもそのまま保存されていた不具合を根本解決。
    - **プロンプトでのサーバーニックネーム・ユーザーID明示とスキーマ指示**:
      - `update_user_habit_profile` に `user_name` および `user_display_name` 引数を追加し、`ollama_chat_reply_memory.py` から確実に伝播。
      - `HABIT_PROFILE_SCHEMA` の `reason` プロパティの description にサーバーニックネーム・ユーザーID主語指示を追加。
      - `system_prompt` および `prompt` に対象ユーザーのサーバーニックネーム（『〇〇』さん）およびユーザーIDを明記し、判断理由（`reason`）は必ずサーバーニックネーム（またはID）を主語として記述するよう指示。
    - **事後正規化ガード（多重防御）**:
      - `call_ollama_json` の出力直後に `normalize_memory_text` を適用し、LLMが万一「ユーザーの発言は〜」「ユーザーは〜」を出力した場合でも、保存前に自動的に `[uid:12345] 『〇〇』さんの発言は〜` へ置換・正規化する多重防御を配備。改名履歴（`aliases`）も取得して渡し、旧ニックネームがあれば最新名へ動的置換。
67. **定期内省（Reflection）における休眠・未対話メンバーの個別選定誤爆防止（アクティブユーザー限定）、チャンネル全体内省での話者ラベル欠落・第三者名推測捏造禁止、および会話抽出時の対話相手と第三者・スレ名の主客分離（項目130）**:
    - **課題の本質**:
      - 最近別のユーザーがBotと会話した内容（シャニマス推し、透・円香推し、古巣やimgのシャンクスレへの拒否反応等）が、ここ1年発言していない休眠メンバー『スーパーシャンクス』の信念・文化として定期内省ログに出力・保存されてしまう重大な主客転倒不具合を是正。
    - **休眠ユーザー・未対話メンバーの完全除外（アクティブユーザー限定選出）**:
      - `ollama_bot/ollama_chat_/agent/reflection.py` の `_run_reflection_once` において、`user_relationships.last_interaction_ts` を参照し、直近（`REFLECTION_USER_ACTIVE_DAYS`、デフォルト7日）以内に対話実績があるアクティブユーザーのみを個別内省対象（`subject_kind="user"`）として選出するアクティブ判定を実装。1年間発言していない休眠メンバーや未対話メンバー、Botアカウントを物理的に完全除外。
    - **チャンネル全体内省における話者ラベル付与と第三者名推測捏造の厳禁**:
      - `build_reflection_memory_lines` に `user_label_map` を追加し、チャンネル全体の内省（`subject_kind="agent"`）時に各記憶レコードへ話者タグ（`[〇〇さん]`）を明記。
      - `build_reflection_prompt` およびプロンプトガイダンスにおいて、話者が明記されていない記憶に対して第三者やサーバーメンバーの名前を推測・捏造して主語に当てはめることを明示的に禁止。
    - **会話抽出時における対話相手と第三者・スレ名の主客厳格分離**:
      - `harness/agent/memory_manager/extractor.py` のシステムプロンプトおよびユーザープロンプトにルール6を追加し、対話相手本人の好み・主張（推し等）と、会話の話題に出た第三者（別メンバーやスレ名）を混同して主語をすり替えて保存することを厳重に禁止。
68. **フェーズ1（コンテキスト構築）における直前会話ログ行（Assistant:, System: 等）の長期記憶リスト混入防止、事前分析LLMによるAI直前台詞誤選定遮断、および重複ガード（フェーズ4）拒絶・フォールバック転落の根絶（項目131）**:
    - **課題の本質**:
      - ユーザーからのボケ・挨拶等の発言（「🦵コイトキック♪」等）に対し、Botが `OLLAMA_SIMILAR_REPLY_FALLBACK`（「ぴゃっ…！さっきと同じようなことを言っちゃいそうだったんですよ…！」）を出力してフォールバックに転落する問題を根本解決。
    - **会話ログ行のマージ遮断と記憶サニタイズ**:
      - `_generate_break_reply_from_memory` において、要約生成用の会話ログ行（`runtime.memory_context_lines`）が長期記憶候補（`raw_memory_lines`）に混入していたバグを修正し、マージ対象を `runtime.recalled_topic_memories` のみに限定。
      - `harness/agent/prompt_parser/prompt_manager/budget.py` に `is_chat_log_or_system_line`, `is_assistant_echo_memory_line`, `sanitize_memory_candidates` を配備。会話ロール行（`Assistant:`, `System:`, `User:`, `Bot:`, `AI:`, `あなた:`）や内心のメモ、直前発言類似行（>= 0.70）を確実に除外。
    - **事前分析LLM前後の多重防御サニタイズ**:
      - `_analyze_pre_reply_context` に渡す候補メモリ一覧から、会話ログ形式の行や直前アシスタント発言（`last_assistant_text`）と一致・類似する行を事前サニタイズで自動除外。
      - さらに、LLM推論後の選定結果（`selected_lines`）に対しても事後サニタイズを適用し、万が一会話ログや直前発言が選定された場合でもメインプロンプトへの注入を物理的に遮断。
    - **リトライ時プロンプトスリム化の強化**:
      - `_slim_base_prompt_for_retry` において、`_clean_memory_section_for_retry` を配備し、`直前のAI発言:` ブロックの除去に加えて、`この相手の過去の記憶や参考情報:` ブロック内に紛れ込んだ `Assistant:` 行や直前発言の引き写し行も確実に除去。リトライ時の推論モデルのコピペ・連続失敗を防止。

---

## 🏛️ システム・アーキテクチャの前提

1. **単一プロセス・独立Bot**:
   - AIチャット（Ollama）、感情状態、記憶システム、ゲーム、外部ツールはすべて**単一の `commands.Bot` プロセス**で稼働します。
   - 同一トークンで別プロセスを並行稼働させてはなりません（Interaction競合 `10062` やコマンド消去の原因）。
   - コマンド同期は `setup_hook()` で1回だけ行います（グローバルコマンド同期＋旧ギルドコマンドのクリーンアップ）。
   - **同一トークン複数コンフィグ運用時のサーバー別Bio分離（Guild Member Bio）**: 同一トークンで複数コンフィグを並行稼働させる場合、各Botは担当サーバー（`resolve_bot_guild_id()`）を特定し、Discord API のサーバー別メンバープロファイル（`PATCH /guilds/{guild_id}/members/@me`）に `bio` を送信します。これにより同一トークンであっても各サーバーで独立した自己紹介が表示され、他サーバーの自己紹介を上書きしません（未指定時・エラー時は `PATCH /applications/@me` にフォールバック、項目124）。

2. **オブジェクトのスコープと責務分離**:
   - `OllamaChatCog` (`self`): 感情状態（`self.emotion_state`）、関係値（`self.user_relationships`）、記憶ストア（`self.memory_store`）、返答キューなどの永続状態を保持。
   - `MessageRuntime` (`runtime`): 1つのメッセージ処理サイクル限りの短期的実行時データコンテナ。
   - ⚠️ `runtime.emotion_state` は**存在しません**。感情や状態の操作は必ず `self` 経由で行い、データクラスを勝手に拡張しないでください。

3. **非同期イベントループとタイムラグ制約**:
   - Discord API の Interaction 有効期限は **3秒** です。
   - `setup_hook()` で重い処理を同期実行してブロックしてはなりません。重い初期化は `on_ready` 以降の非同期タスクで行います。

4. **外部依存は非信頼境界**:
   - Ollama LLM、天気API、Chrome MCP、Web検索は常にタイムアウトや障害が発生しうる前提で設計し、多重防御とフォールバックを組み込みます。

5. **モジュール分割とファイルサイズ制約 (50KB Rule)**:
   - ソースコードファイルは原則 **50KB 以下** を維持します。
   - 肥大化したモジュールはパッケージディレクトリ（`agent/`, `harness/agent/memory_manager/`, `emotion/`, `chat_prompt/`, `ollama/`, `reply_safety/` 等）へ責務別に分割します。
   - ハーネスアーキテクチャ（`harness/`）への移行が完了したドメイン（記憶マネージャー `harness/agent/memory_manager` 等）については、旧パスの後方互換スタブを完全に廃止・物理削除し、直接参照へと一本化します。各責務別モジュール（`storage/`, `extractor`, `retriever`, `summarizer`, `logic` 等）はすべて 50KB 未満（最大 24KB 以下）を厳格に死守します。

6. **共通ユーティリティ基盤 (`lib/`) と後方互換性の維持**:
   - プロジェクト全体で横断的に利用される低結合・高凝集な共通基盤コードは、リポジトリ直下の `lib/` フォルダ配下に集約します。
   - 主な共通モジュール:
     - `lib/json_utils.py`: 高速JSON処理（`orjson` フォールバック）、非同期読み込み、アトミック書き込み（`save_json_atomic`, `save_json_atomic_async`）。
     - `lib/config_utils.py`: 設定値・環境変数パーサー（`cfg`, `cfg_str`, `cfg_bool`, `cfg_int`, `cfg_float`, `cfg_int_set`, チャンネルID抽出、モデル推論機能判定等）。
     - `lib/date_utils.py`: タイムゾーン定義（`JST = ZoneInfo("Asia/Tokyo")`, `UTC`）、現在日時（`now_jst`, `now_utc`）、タイムスタンプ・書式化関数（`format_jst_datetime` 等）。
     - `lib/text_utils.py`: 全角半角正規化（`normalize_text`）、比較用正規化（`normalize_compare_text`）、感嘆詞トリム（`strip_leading_interjections`）、実質第1節抽出（`extract_effective_first_clause`）、URL抽出・除去（`extract_urls`, `strip_urls`）、文字数・行数切り詰め（`truncate_text`, `truncate_lines`）。
     - `lib/discord_utils.py`: メンション・入力テキスト正規化（`strip_discord_mentions`, `clean_user_input_text`）、絵文字正規化（`normalize_emoji_str`）、Discordオブジェクト安全アクセス（`author_id`, `channel_id`, `guild_id`, `content`, `display_name`, `snowflake_to_ts`, `message_created_at_ts`）。
   - 各機能モジュール内で独自に `ZoneInfo("Asia/Tokyo")` を定義したり、設定パース処理や一時ファイルアトミック書き込みを重複実装してはなりません。
   - 標準ライブラリ（`zoneinfo`等）や `lib/` への移行に伴い不要となった外部パッケージ（`pytz` 等）は、`requirements.txt` から除外するだけでなく、設定ファイル（`config_base.py` 等）を含む全コードから完全にインポートを根絶してください（`requirements.txt` 未定義ライブラリのインポート残骸禁止）。
   - 既存の共通モジュール（`ollama_bot/common/config_helpers.py` 等）を `lib/` へ移行する際は、外部・既存コードからのインポートを破壊しないよう、必ず **後方互換アグリゲーター**（再エクスポート）として維持してください。

7. **会話文脈（Context）と返信ツリーの最優先保全（Anti-Hijack & Tree Isolation）**:
   - Discord返信（Reply）時は、チャンネルの直前雑談ではなく**返信先ツリー親メッセージ（`latest_ref`）を最優先文脈**として保護します。
   - 並行する他ユーザーの別話題（寿司の話など）から目的語や固有名詞を混入させる「目的語ハイジャック」を絶対に起こしてはなりません。
   - プロンプト構築時、返信元発言（`【返信元の相手発言】`）と最新発言（`対象メッセージ: {clean_user_text}`）を明確に分離し、AI自身の過去発言とユーザー発言を合成させないでください。

8. **会話中の不明点調査（3段階フォールバック）と自然対話原則**:
   - ユーザーとの会話中に不明な点や評判の確認が必要になった場合は、以下の3段階を厳守します：
     1. **記憶DB（MemoryStore）検索**: 過去の会話や体験から思い出す。
     2. **Web自然調査（Topic Investigation）**: 記憶になければWeb検索で世間の評判・実態を確認する。ただし、ファクトチェックや解説レポートのような硬い形式は絶対に使わず、1〜2文の自然な会話・ネット評判として返答する。
     3. **不知・不確実性の表明**: それでも分からなければ知ったかぶりをせず、「わからない」「確認できた範囲では」と素直に伝える。
   - 「約束」「守って」「使わない」等のルール・行動指針文脈をトピック評価問い合わせと誤認してWeb検索を誤爆させてはなりません。

9. **オウム返しガードの受容保護とリトライプロンプトの完全性保証**:
   - 短い単語・名詞・ボケ（20文字以下）に対して「〜か…！」「〜とは…！」等の感嘆・ツッコミ語尾（`_is_short_noun_reaction_clause`）で受容し、その後ろに十分なリアクション（20文字以上）を展開している返答はオウム返しと誤認しません。ただし、全体丸写し・高類似度（0.85以上）は確実にオウム返しとして検知する二重防御を維持します。
   - リトライ時のプロンプトスリム化（`_slim_base_prompt_for_retry`）において、ユーザー間返信の `【会話の状況（ユーザー同士のやり取り）】` ブロックを丸ごと消去してはならず、メタ注意文のみを除去して `対象メッセージ` を死守します。
   - すべてのリトライプロンプト末尾に思考抑止指示（`CHAT_REPLY_OUTPUT_SUFFIX`）を徹底適用し、推論モデルの思考迷走やModelfile例文誤認を根本遮断します。

10. **コンフィグ駆動（Config-Driven）原則と特定固有人格ハードコードの厳禁**:
    - Botのキャラクター性、一人称、語尾、フォールバック文言、正規表現パターン、除外キーワード等の人格依存要素は、すべて `config.py`（`config_img.py` や `config_shanks.py` 等の各コンフィグファイル）側で設定します。
    - スクリプト共通層（`ollama_bot/` や `lib/` 配下のモジュール群）に特定の人格名（コイトフクマル、小糸、シャンクス等）や特定の口調・語尾（「ぴゃっ」「なんですよ」「だろ」「おれァ」等）をハードコードして分岐させてはなりません。
    - 「テストをパスさせるため」「未設定時の後方互換」という名目で、共通層に関数引数（`persona` 等）によるハードコード分岐や特定人格用のフォールバック文言を記述することは固く禁じます。テストは必ず `patch("lib.config_utils.cfg")` を用いて設定値注入を検証してください。
    - 共通層は必ず `cfg(...)` による動的解決を行い、未設定時は中立な汎用フォールバック（「確認できませんでした」等）を提供してください。コード内のコメントやプロンプト例示であっても特定キャラクターを前提とした固定的な記述を避けてください。

11. **設定提示・解説文の受容とリトライ時直前発言コピペ遮断（Explanation Reception & Anti-Echo Loop）**:
    - ユーザーが用語やキャラクター・作品の設定や解説（『〜とは、〜の登場人物』等）を提示してくれた場合、文中の単語に『その名前は知らない』と辞書的確認やシラ切りで会話を拒絶することを禁止し、提示された設定やエピソードに素直に興味・感想・リアクションを述べます。
    - 類似・オウム返しリトライ時、プロンプトスリム化（`_slim_base_prompt_for_retry`）で `直前のAI発言:` や質問注記を完全に除去し、小型推論モデルが手元の直前発言をそのまま丸コピペ出力する事態を根本遮断します。
    - リトライ結果（`retried`）に対しても直前発言との同一・酷似やオウム返しを再検証し、問題があればキャラクター口調の安全フォールバックへ倒す多重防御を維持します。

12. **サーバーメンバー言及・人物評におけるメタ困惑排除と具体的キャラクター人物像の言語化（Third-Party Representation & De-Excusing）**:
    - サーバーメンバーについての印象や感想を質問された際、過去の断片的な一時的困惑メモ（「急に名前を聞かれて〜」等）を恒久的人物所感（`thought`）へ保存したりプロンプトへ注入してはなりません（`is_meta_confusion_thought` で完全排除）。
    - 関係性パラメータ（親愛度、信頼度、憎しみ等）から、その相手が自分にとってどのような存在か（例: 「いつもからかわれて警戒している相手」「心を開いて安心できる常連さん」等）の具体的キャラクター人物像（`describe_relationship_for_third_party`）を自動言語化してプロンプトへ注入し、対話相手と話題の第三者を厳密に分離します。
    - 返信プレフィックスは `re.DOTALL` で完全に除去し、意見質問（「どう思う？」等）で自発言ツッコミ（`is_self_tsukkomi`）が誤爆して過去発言の言い訳だけで返答を終える事態を防ぎます。

13. **挨拶・自己紹介プロトコル保護と感情コンテキスト引きずられ防止（Greeting Safe-Zone & Emotion Isolation）**:
    - 初対面の挨拶や自己紹介（「初めまして、〇〇と言います。仲良くしましょう」）に対して、AIが「初めまして、〇〇さん！仲良くしましょうね…！」と挨拶や名前を呼び返して応答する自然な対話プロトコルを、冒頭トピックエコーや共通文字比率（`reused_ratio >= 0.55`）でオウム返しと過剰検知してフォールバック台詞へ転落させてはなりません。
    - 直前会話のセクハラ・攻撃的発言等によってBotの内部感情が極度の警戒・恐怖（`fear` 等）状態になっていたとしても、別ユーザーや新しい友好的挨拶（「初めまして」「こんにちは」等）に対して警戒心を引きずって `BAD` リアクション（`:Badmood:` 等）を誤爆付与してはなりません（コード側の安全抑止ガード `_is_friendly_or_greeting_message` ＋感情分類器プロンプトガイダンス）。

14. **サブチャンネル（Other Channel）の確率応答プロトコルとサイコロ方式の厳守（Dice-Roll Isolation）**:
    - `OTHER_CHANNEL_IDS` で指定されたサブチャンネルにおけるBotの自発的応答判定（`_should_respond_in_other_channel`）は、**メッセージ1通ごとに独立して `1 / random.randint(MIN, MAX)` の確率でサイコロを振る方式（サイコロ方式）**を厳守します。
    - 「N件溜まるまで未返信カウントを進めるカウンター方式」や、メンション・返信（`force_response`）によるカウンターリセット機構を導入してはなりません（少人数会話で完全沈黙し、ユーザーからBotが壊れたように見える原因になります）。
    - チャンネルIDの判定（`_is_target_channel_id`）は、環境変数のカンマ区切り文字列（`"123,456"`）等にも安全に対応できるよう、必ず `lib.config_utils.cfg_int_set("OTHER_CHANNEL_IDS")` を用いて整数セットとして照合します。

15. **プロンプト対象メッセージの二重話者名排除と話者名付きエコー行サニタイズ（Anti-Speaker Echo & Double-Prefix Guard）**:
    - チャンネル全体の雑談・サブチャンネル会話への自発参加プロンプト（`builder.py`）において、`発言者: {display_name}` を明記しているブロック内で `対象メッセージ:` に `{display_name}: {content}` と二重に話者名を付与してはなりません（LLMが台本形式と誤認して相手の発言を先頭行に切り取って出力する誘引となります）。
    - 出力サニタイザー（`_strip_user_echo_prefix`）では、純粋なユーザー発言の前方一致だけでなく、話者名プレフィックス形式（`re.match(r"^([^:\n]{1,40})[:：]\s*(.*)$")`）が付いたエコー行も確実に検出し、先頭エコー行と後続の空行を除去して本来のセリフのみを救出する多重防御を維持します。

16. **質問・意見照会におけるサーバーメンバー最優先照合（User-First Resolution）**:
    - ユーザーから「〇〇についてどう思う？」「〇〇って誰？」等と質問された際、対象名詞が一般名詞や作品名等と被る場合であっても、まず最初にDiscordサーバーメンバー一覧・プロファイルDBとの照合を最優先（User-First）で行います。
    - サーバーメンバーに該当する場合は、一般トピック向けのWeb検索パイプラインを即座にバイパスし、仲間としての関係性パラメータや人物像（`describe_relationship_for_third_party`）を最優先でプロンプトに注入します。サーバーメンバーに該当しないことが確認された場合に初めて、Web自然調査や一般名詞解釈へフォールバックします。

17. **自立話題・質問における記憶検索クエリ自立化とトピック隔離（Topic Isolation）**:
    - 指示語（これ、それ等）を持たない新規の疑問・質問・挨拶等は、短文（14文字以下）であっても無条件の話題継続（FOLLOW）と判定せず、自立話題（`is_standalone_topic`）として厳格に扱います。
    - 記憶DB（MemoryStore）検索において、自立話題の場合は直前AI発言を強制合成せずユーザー発言単体で検索し、無関係な過去記憶の混入を防止します。
    - 自立話題時のプロンプト構築では、直前AI発言、過去の会話履歴、チャンネル会話要約を物理的に完全除外し（トピック隔離）、小型推論モデルが手元の直前発言単語を拾って前の話題を引きずる現象を根本遮断します。

18. **ユーザー教示文におけるWeb検索遮断と話題転換（BREAK）時長期記憶想起保全・無関係記憶全選択ガード（Teaching Protection & Break Memory Preservation）**:
    - ユーザーが知識や設定（「〜とは、…です」「〜の名称です」「覚えてください」等）を教えている教示・定義文（`looks_like_user_teaching_or_definition`）では、文中の「とは」や年号・日付に引きずられてWeb検索（`needs_research`）を誤爆起動せず、対話・記憶保存フローを優先します。
    - 直前会話と異なる話題（BREAK）かつWeb調査の判定となった場合でも、専用プロンプト関数で記憶検索を蒸発させず、`_generate_break_reply(..., skip_model=needs_research)` を統一経由させて過去の記憶を確実にプロンプトへ組み込みます。
    - 小型推論モデルが候補メモリを無差別に全選択した際、教示内容のキーワードと直接一致しない過去記憶を事後フィルター（`_post_filter_selected_memories`）で確実に除外し、無関係な過去キャラクターや設定の記憶誤爆（混同）を根本遮断します。

19. **特定ユーザーのコマンド実行制限（COMMAND_IGNORE_USER_IDS）と通常チャット会話の保全（Command Isolation & Chat Preservation）**:
    - 通常のAI雑談・チャットは許可したまま、スラッシュコマンド、UIボタン・モーダル、プレフィックスコマンド（`!` / `！`）、管理者コマンド等のコマンド機能のみを特定ユーザーに禁止する機能（`COMMAND_IGNORE_USER_IDS`、エイリアス: `IGNORE_COMMAND_USER_IDS`）を整備します。
    - 会話も完全に無視する `IGNORE_USER_IDS` と明確に分離し、Interactionは `on_interaction` および `tree.interaction_check` でエフェメラル通知（「❌ コマンド機能のご利用は制限されています。」）、テキストコマンドは `@bot.check` および `_handle_admin_text_command` で遮断しつつAI返信キューへの誤爆流入を抑止します。

20. **ハーネス・アーキテクチャ（Harness Architecture）の階層構造と責務分離**:
    - 本Botの知能・行動基盤は、明確なハーネス階層ツリー（実行基盤・評価安全性基盤）に基づいて設計・運用されます。
    - **1-1. プロンプトマネージャー（フェーズ1）＆ パーサー（フェーズ2）**:
      - 配置場所: `harness/agent/prompt_parser/`
      - **PromptManager (`prompt_manager/`)**: システムプロンプト動的生成（`system_prompts.py`）、日時・中間LLM分析プロンプト（`analysis_prompts.py`）、リトライ時プロンプトスリム化（`retry_prompts.py`）、文脈・記憶統合（`builder.py`）、トークン予算管理（`budget.py`）、意図・文脈事前分析（`analyzer.py`）、スレッド追跡（`thread_tracker.py`）を一元管理。
      - **OutputParser (`parser/`)**: LLM出力からのJSON構造化抽出・スキーマ検証（`structured.py`）、思考タグ・内部指示・推論漏洩除去（`cleaner.py`, `control.py`, `patterns.py`）、未完思考ブロックからのセリフ安全救出（`salvage.py`）、出力安全性・品質検証（`validation.py`）を一元管理。
      - 共通コードやフォールバック層に個別の人格・プロンプト断片を直書きすることを禁じ、すべて `PromptManager` および `OutputParser` を通じて安全に処理します。

21. **プロンプト処理・RAG統合パイプライン仕様（Prompt Processing & RAG Integration Pipeline Specification）**:
    - ユーザーからのプロンプト受付からAI応答出力に至る処理は、以下の8ステップからなる決定論的・多層防御パイプラインとして実行されます：
      1. **入力正規化とスレッド特定（Normalization & Thread Tracking）**:
         - メンションや入力ノイズの正規化（`lib.discord_utils`）、会話スレッドの特定・並行スレッドの分離（`ThreadTracker.match_thread`）、リプライチェーン（`collect_reply_chain`）による返信先親メッセージの同定。
      2. **短期記憶（Short-term Memory）の抽出と文脈保護**:
         - チャンネル直近の会話履歴（`context_lines`）をキャッシュから取得。返信ツリーが存在する場合は親メッセージ（`latest_ref`）を最優先文脈として保護し、並行する無関係な他ユーザー雑談からの目的語ハイジャックを遮断。
      3. **長期記憶/RAG（Long-term Memory & Episodic Retrieval）の検索**:
         - 自立話題（`is_standalone_topic`）判定を行い、話題引きずりを防ぐ検索クエリを生成。
         - 埋め込みモデル（BGE-M3, 1024次元）によるベクトル類似度検索＋FTS5全文検索で `MemoryStore` から候補記憶（`memories`）、出来事記憶（`memory_episodes`）、未来の意図（`future_prompts`）を想起。
      4. **事前知識・言及人物の事前解決（User-First Resolution & Entity Linking）**:
         - 会話中に登場するサーバーメンバー名を最優先照合（`resolve_and_format_mentioned_users`）。関係性パラメータ（親愛度、信頼度、憎しみ等）から具体的キャラクター人物像（`describe_relationship_for_third_party`）を言語化し、対話相手と話題の主を厳密分離。
      5. **事前分析エージェント（Middle-LLM Context Analyzer）による選別・内省**:
         - 軽量構造化モデル（`middle-llm`）を起動し、①意図判定（`intent`）、②AI言及判定（`target_is_ai`）、③**メモリ選別（`selected_indices`）**（候補の中から今回の返答に真に関連する記憶のみを抽出）、④内省補助（`emotion_focus`, `reply_strategy`, `avoid`）、⑤自己整合性監査（`persona_risk`, `consistency_correction`）、⑥会話破綻修復（`is_breakdown`）、⑦不確実性（`uncertainty_level`）を1回のJSON呼び出しで判定。
      6. **システムプロンプトの動的生成（Dynamic System Prompt Generation）**:
         - 現在日時（サーバ時刻JST）、キャラクター設定（一人称、口調、世界観）、口調ガード、返答形式、時間帯補足を `system_prompts.py` で動的に組み立て。
      7. **コンテキスト予算管理とプロンプト結合（Budgeting & Prompt Assembly）**:
         - トークン予算（`_trim_prompt_inputs`）に基づき、短期記憶・長期記憶・要約・エピソードを安全にトリム。
         - 現在の感情状態（`EmotionState`）、相手との関係性（`UserRelationship`）、核となる価値観（Core beliefs）、選別済み長期記憶、短期履歴、返信ツリー、言及メンバー情報、対話相手情報、対象メッセージ、内省メモを `builder.py` で結合。
      8. **推論実行と出力パーサーによる多重防御（Inference & OutputParser）**:
         - メインモデルで応答生成。思考タグの未完切断時は `salvage` でセリフを救出。
         - `OutputParser.sanitize` で思考タグ、制御行、指示漏れ、箇条書き、Markdown、相手誤認呼びかけ、Bot自己メンション、話者名付きエコー行を徹底除去。
         - `OutputParser.validate_safety` でオウム返し、文末途切れ、外国語、前発言酷似、キャラ崩壊、時間帯矛盾等を検証し、安全な対話応答のみをDiscordへ送信。

22. **エージェント推論・行動ループとMCPツール連携アーキテクチャ（Agent Loop & MCP Integration Architecture）**:
    - 本パッケージ（`harness/agent/prompt_parser/`）は、ツールレジストリ（`harness/agent/tool_registry/`）およびMCPサーバー群（`tool_registry/mcp/`）と連携し、以下の3段階エージェントループを自律的に遂行します：
      1. **コンテキストの構築（知覚・記憶の引き出し & ツールカタログ動的取得）**:
         - ユーザーからのプロンプト受信時、会話履歴やRAG記憶（`MemoryStore`）に加え、`ToolContextManager` を通じて外部MCPサーバー（気象庁・ウェザーニュース・Chrome等）やツールレジストリから「現在利用可能なツールの説明書（スキーマ・引数定義）」を動的にかき集め、システムプロンプトに結合してLLMに渡す準備を整えます。
      2. **推論と計画立案（Thought & Planning）**:
         - 記憶とツールカタログで構成されたコンテキストを元に、LLMが「目的を達成するために今何をすべきか」を思考（Thought）します。
         - 情報検索や外部干渉が必要と推論した場合、渡されたツールカタログから最適なツールを選択し、「このツールを、この引数で実行する」という具体的な計画（JSON CallブロックまたはReAct形式）を出力します。
      3. **ツール選択と実行（Action & MCP JSON-RPC Dispatch）**:
         - LLMが出力した関数呼び出し指示をパーサー（`OutputParser.parse_tool_calls`）が解釈し、思考（Thought）とツール呼び出し（`ToolCall`）を抽出・分離します。
         - 指示されたツールがMCP経由のものであるとパーサー（`is_mcp_tool`）が解釈した場合、オーケストレーター（`execute_action`）はMCPクライアントを介して対象のMCPサーバーへ JSON-RPC プロトコルの CallTool（`call_tool`）リクエストを送信します。
         - サーバー側で実際の処理が行われ、その結果が Observation（`ToolResult.to_observation_message`）としてエージェントに返却され、次の推論または最終回答の生成へと繋がります。

23. **小型分類LLM（middle-llm）によるMCP利用判断と共感誤爆ガード（Middle-LLM MCP Routing & Empathy Safeguard）**:
    - メイン対話モデルに常時長大な全ツール定義を読み込ませるオーバーヘッド（トークン浪費・レイテンシ増大）や誤爆を防ぐため、前段の小型分類専用モデル（`middle-llm:latest`、Qwen3.5-9B、`temperature: 0`、`num_predict: 256`）がMCPツール（天気・Chromeブラウザ・Web検索・ふたば・X等）の利用要否（`needs_mcp`）およびツール種別（`mcp_category`）を決定論的に分類・ルーティング（`classify_mcp_routing`）します。
    - **判断基準（Criteria）**:
      - ① **天気MCP (`weather`)**: 地域名・日時の天気予報、気温、降水確率、傘の要否、台風情報の「質問・照会」に対して起動（`jma_get_weather`, `weathernews_get_forecast`）。
      - ② **Chrome MCP (`chrome_browser`)**: URL（http/https）が提示され、該当ページの本文読解・要約・画面操作が求められている場合に起動（`chrome_read_url` 等）。
      - ③ **ふたばMCP (`img2chan`)**: ふたば（`img.2chan.net`）のスレッドURL読解、または勢い上位スレッドの確認要求時に起動。
      - ④ **X MCP (`x_timeline`)**: 「今のXのトレンド」「タイムライン見てきて」など現在のX上の動向照会時に起動。
      - ⑤ **Web検索・事実調査MCP (`web_search`)**: 最新ニュース、製品スペック、発売日、未知語、出典・ソース要求時に起動。
      - ⑥ **MCP不要 (`none`)**: 挨拶、日常雑談、共感、相槌、ボケ・ツッコミ、相談、創作など、内部知識で十分返答できるもの。
    - **共感誤爆防止・多重防御ガード**:
      - 「今日暑いね〜」「雨降ってて憂鬱」のような天候への感想・共感雑談に対し、機械的に天気APIを叩いて会話のキャッチボールを壊さないよう、質問表現を伴わない天候共感発言は事後サニタイズで `needs_mcp=false` に自動補正。
      - Discordサーバーメンバーへの質問（ブルードラシャン等）や内輪の話題は外部MCP/検索へ回さず、User-First解決を最優先。
      - 分類モデル呼び出し例外時は、URL有無や明示的質問キーワードに基づくルールベースフォールバックにより安全に縮退稼働。



---

## 🧭 コア開発原則 (Engineering Principles)


1. **原則1: 事前定義の確認（Look Before You Leap）**
   - コード修正前に `ollama_chat_types.py` や Mixin の実装を `view_file` 等で確認する。推測でプロパティやメソッドを呼ばない。
2. **原則2: 副次効果・パイプラインの完全保全（Preserve Side-Effects & Event Pipelines）**
   - フロー変更時に Discord Presence / ニックネーム同期 / リアクション付与 などの副次効果を脱落させない。
   - 返信ツリー親文脈の保護や、ユーザー訂正に対する即時自己修正（直前発言への固執排除）などの対話パイプラインを破壊しない。
3. **原則3: 多重防御と安全なフォールバック（Defensive Programming & Graceful Degradation）**
   - 外部通信は `try...except (aiohttp.ClientError, asyncio.TimeoutError, Exception)` で包括的に捕捉し、フォールバック応答へ安全に倒す。
   - 不明点調査の3段階フォールバック（記憶DB -> Web自然調査 -> 不知表明）や、リトライ時プロンプトにおける対象メッセージ自動復元・思考抑止（`CHAT_REPLY_OUTPUT_SUFFIX`）の多重防御を徹底する。
4. **原則4: 堅牢な状態永続化と時間減衰（Atomic Persistence & Time Decay）**
   - 感情や関係値のディスク保存は一時ファイル経由のアトミック書き込み（`flush` -> `fsync` -> `replace`）を徹底する。起動時はオフライン経過時間による感情減衰（`neutral` 回帰）を行う。
5. **原則5: 入力の厳格な正規化と前処理（Strict Input Normalization）**
   - パラメータ抽出前にメンション、前置き接続詞（「じゃあ」「では」等）、日付、助詞を確実にトリミングする。静的データはプリセットキャッシュを活用。
   - オウム返しガードでは発言文字数・受容語尾・類似度に応じた多層判定を徹底する。
6. **原則6: Discord API 制約の絶対厳守（Discord API Constraints Compliance）**
   - 1ギルドあたりの Message Context Menu は最大5個。
   - 単一設定ID（`GUILD_ID`）に依存せず `bot.guilds` へのフォールバックを用意する。
7. **原則7: 静的型付けと mypy 厳格運用の徹底（Static Typing & Type Safety First）**
   - すべての引数・戻り値・フィールドに Python 3.11+ 型アノテーション（PEP 484/585/604）を付与する。
   - コード変更後は必ず `mypy` 検査を実行し、エラー 0 件（`Success: no issues found`）を維持する。

---

## 🚫 禁止事項クイックインデックス (Ban List Summary: 全117項目)

作業領域に応じた禁止事項の詳細は [.agents/rules/ban_list.md](file:///Users/Shadow/Documents/05%20%E9%96%8B%E7%99%BA/discord-ai-bot/.agents/rules/ban_list.md) を参照してください。

### 🧠 1. LLM推論・プロンプト設計・サニタイザー
- ❌ **項目15**: 推論モデル（Qwen等）の `think=False` デフォルト固定および `num_predict` 不足の禁止
- ❌ **項目20**: 非思考モード時のメタ受諾・ルール宣言（「このルールは絶対」等）漏洩および `<think>` 前方サニタイズすり抜けの禁止
- ❌ **項目21**: 内部メモ区切りの会話的接続詞（「これらを踏まえて」等）エコーおよび同一行プレフィックスすり抜けの禁止
- ❌ **項目23**: 特殊アクションのプロンプト制約矛盾（ですます厳禁等）・過密重複・文字数縛りおよび思考トークン不足の禁止
- ❌ **項目24**: チャット返答プロンプト末尾の思考抑止指示（`CHAT_REPLY_OUTPUT_SUFFIX`）欠落および長文思考暴走の禁止
- ❌ **項目28**: トークン不足（`done_reason=length`）による文末途切れ未検知送信およびリトライ上限延長失敗の禁止
- ❌ **項目34**: 思考タグ（`<think>`）サニタイズにおける `</think>` 前方テキストの安易な保持・結合およびプロンプト思考抑止対症療法の禁止
- ❌ **項目35**: 特殊アクションにおける直接出力指示欠落・過剰事実縛りによる思考迷走およびリカバリーなし即時フォールバックの禁止
- ❌ **項目39**: 歌唱・連続投稿におけるLLMダミー連番（行1、line 1等）のバリデーション漏れオウム返しおよび機械的・単調な固定ディレイ送信の禁止
- ❌ **項目41**: プロンプト多重重複ルールによる推論モデルの思考迷走ループ（Self-Correctionトークン枯渇）および未完思考ブロック（`<think>` 切断）のセリフ救出（サルベージ）欠落の禁止
- ❌ **項目42**: Web検索フォールバックにおけるURL属性（href）抽出欠落による歌詞・外部サイト連携の機能不全およびプロンプト否定制約トークンによるLLMオウム返し誘導の禁止
- ❌ **項目44**: オウム返しガード（Parrot Guard）における自然な共感・受容表現（Validate First）の過剰検知による強制リトライの禁止
- ❌ **項目46**: メガネ絵文字（🤓）・豆知識マウントボケにおける真面目解説・受容および自己言及（中学生扱い等）被害妄想の禁止
- ❌ **項目47**: セリフ救出（サルベージ）における異種引用符マッチによる文章途中切断および未閉じ括弧途切れ検知漏れの禁止
- ❌ **項目48**: リトライプロンプトの過密肥大化による推論モデル思考迷走、リトライ結果の再検証パイプライン欠落、および短文英語思考漏洩の検知漏れの禁止
- ❌ **項目52**: 通常生成プロンプト過密による推論モデル思考切断、未完思考ブロックからのプロンプト指示文サルベージ誤爆、およびメタ指示表現（〜してください等）漏洩検知漏れの禁止
- ❌ **項目55**: 自発発言（proactive）におけるプロンプト多重重複による思考切断、および未完思考ブロックからの英語プロンプト分析行・greedy引用符誤サルベージの禁止
- ❌ **項目57**: オウム返しガード（Parrot Guard）における受容文判定順序ミス・読点切断による共感返答の過剰検知、およびリトライ指示による名詞封印・属性捏造（ハルシネーション）の禁止
- ❌ **項目59**: ユーザー訂正に対する直前AI発言の固執・矛盾属性捏造、感嘆詞によるオウム返しすり抜け、および未体験話題でのハルシネーション同調の禁止
- ❌ **項目62**: Discord返信時における親メッセージ混入による話題引きずり、ルール・約束文脈でのトピック評価Web検索誤爆、および承諾返答に対するオウム返しガード誤検知の禁止
- ❌ **項目63**: 短い名詞・共感返答に対するオウム返しガード誤検知による不要リトライ、リトライプロンプトのスリム化漏れ、および英語検証語尾サルベージ誤爆の禁止
- ❌ **項目64**: 短い名詞・ボケに対するオウム返しガード誤検知による不要リトライ、リトライプロンプトのスリム化による対象メッセージ消失、および推論モデルによるModelfile例文誤認・パッチワーク返答の禁止
- ❌ **項目65**: 思考ブロック・サルベージ時における英語検証メモ（`(Matches exactly)` 等）のセリフ混入・クォート外メモ分離漏れ、および文末思考漏洩検知・サニタイズ欠落の禁止
- ❌ **項目66**: リトライ時における直前AI発言丸コピペ出力・再検証パイプライン欠落、および設定・情報提供文に対する辞書的拒絶・シラ切りの禁止
- ❌ **項目67**: 長文返信プレフィックス除去失敗による自発言ツッコミ（`is_self_tsukkomi`）誤爆、および言及サーバーメンバー質問に対する自発言自覚ルールの過剰作用・言い訳返答の禁止
- ❌ **項目68**: 言及サーバーメンバー質問に対する一時的困惑所感（`thought`）のプロンプト誤注入・忠実セリフ化による言い訳返答、および関係性パラメータからの第三者人物像言語化欠落の禁止
- ❌ **項目72**: 初対面・挨拶・自己紹介に対するオウム返しガード過剰検知（フォールバック転落）、および直前不穏文脈の引きずられによる友好的発言へのBADリアクション誤爆の禁止
- ❌ **項目74**: プロンプト対象メッセージへの話者名プレフィックス二重付与によるLLM発言エコー誘発、および話者名付きエコー行のサニタイズ漏れの禁止
- ❌ **項目77**: ユーザー発言に対する理解不能・「わからない」系返答の無理な送信の禁止（スキップ機能）、および言語対応・メインチャンネル・直接メンション例外の保護

### 💖 2. 感情状態・ユーザー関係値・副次効果
- ❌ **項目1**: `runtime.emotion_state` へのアクセス禁止（必ず `self.emotion_state` を使用）
- ❌ **項目5**: 感情ブレンド係数（`0.35`）を無視した低すぎる生スコア設定の禁止（`0.20` 突破不可の防止）
- ❌ **項目6**: フロー変更・同期化に伴う副次効果（Presence/Nickname/Reaction）の脱落禁止
- ❌ **項目8**: 感情・関係値の非同期投げっぱなし保存（`create_task`）および起動時経過時間減衰無視の禁止
- ❌ **項目16**: 絵文字リアクション付与時の会話行・キャッシュ・返答プロンプトへの自己認知連携漏れの禁止
- ❌ **項目22**: ユーザー関係値（`thought`/`last_interaction_ts`）の永続化フィールド欠落・アトミック保存省略・`/relationship` コマンド分散の禁止
- ❌ **項目99**: 感情値説明文および返答温度感ガイドラインの文法崩壊防止と自然言語化（動詞句の生結合・「〜にじむ、かつ〜気にするの温度感」の根絶）
- ❌ **項目102**: 高憎しみ・強い反発時の会話拒絶・放棄（「関係ない」等）の禁止とツッコミ・反論誘導（キャラクター性と対話継続の両立）
- ❌ **項目114**: 感情評価コンテキストにおける内部メモ・記憶の「相手発言」誤認排除、事前分析・関係値ブロックへの対話相手名明記と第三者混同防止、および返答生成プロンプト冒頭での対話相手早期宣言（被害妄想・過剰敵対心・不知拒絶の根絶）

### 📚 3. 記憶システム・コンテキスト・ユーザー関心事・プロファイル追跡
- ❌ **項目17**: 記憶検索における仮想テーブル（`vec_memories`/`fts_memories`）同期漏れ・多層フォールバック破壊・FTS5未サニタイズの禁止
- ❌ **項目18**: 体験否定短文（「見てない」「知らない」等）における直前話題コンテキスト破棄およびメッセージ不達/存在否定メタ誤認の禁止
- ❌ **項目19**: ユーザー関心事（`user_interests`）の抽出・蓄積・減衰および自発発言話題提供パイプライン破壊の禁止
- ❌ **項目25**: ユーザープロファイル・名前変更履歴（`aliases`）の永続化欠落および会話内人物言及の誤認・ツッコミ暴走の禁止
- ❌ **項目26**: 複数人会話における他ユーザー発言へのツッコミ文脈遮断およびAI自己言及・被害妄想誤認の禁止
- ❌ **項目27**: 短文・相槌における記憶選別スキップ時の無条件記憶注入（記憶誤爆）および自己整合性監査（`consistency_correction`）への話題・事実指示混入の禁止
- ❌ **項目40**: 複数人会話での割り込み・因果接続詞における文脈判定（FOLLOW/BREAK）誤認および三人称状況説明のAI要求（`target_is_ai`）誤認の禁止
- ❌ **項目43**: 複数人会話における言及メンバーの重複注入および対話相手（話者）と話題の主の混同呼びかけの禁止
- ❌ **項目45**: 短文リアクション・ツッコミ・ユーザー間返信における文脈切断（BREAK誤判定）による辞書的確認・話題拒否の禁止
- ❌ **項目49**: リプライ参照先メッセージのキャッシュ強制削除（`remove_context_message`）による会話文脈破壊、クイズ回答・推測発言の主客転倒長期記憶誤爆、および単なる質問・相槌に対するAI依頼要求ルールの過剰注入の禁止
- ❌ **項目50**: 自発発言・自己対応時のネタ元キャッシュ欠落による自己言及・話題フォローアップ機能不全、内心メモの他ユーザー誤認、および自己発言に対する「覚えてない」シラ切り・過剰防衛の禁止
- ❌ **項目51**: ユーザー間リプライにおける二人称代名詞（「君」「お前」等）のAI宛て誤認（`target_is_ai`）および構造化返信関係の欠落による自己弁護暴走の禁止
- ❌ **項目53**: ユーザー間返信におけるプロンプト状況説明（「〇〇さんに聞かれてる」「私じゃなくて」等）のメタ読み上げ、一人称ブレ、および無断メガネ絵文字（🤓）付与の禁止
- ❌ **項目54**: AI自身の属性・プロフィールのユーザー長期記憶への誤保存、プロンプトへの自己記憶注入による主客混同、および対話相手への自己名呼びかけ（「ありがとう、コイト」等）の禁止
- ❌ **項目56**: 第三者発言の伝聞・密告構文（「〇〇は〜と言ってたぞ」等）における主語誤読・直近発言自覚ルールの過剰作用・一人称（私）ブレの禁止
- ❌ **項目58**: Discord返信（Reply）時におけるリプライツリー親文脈の欠落・並行チャンネル雑談による目的語ハイジャック（話題乗っ取り）の禁止
- ❌ **項目61**: 展開質問（「じゃあ〇〇は？」等）における文脈切断（BREAK誤認）からの記憶フロー逸脱、過去ファクトチェック記憶の誤選別・意図誤認、および自己整合性監査への事実汚染の禁止
- ❌ **項目75**: ファクトチェック・外部調査における直前雑談による目的語ハイジャック（無関係な検索クエリ生成）、および基底設定（config_base.py）の派生コンフィグによる無条件上書きの禁止
- ❌ **項目84**: 質問・意見照会におけるDiscordサーバーメンバー最優先照合（User-First Resolution）の欠落および一般名詞・Web検索フォールバック誤爆の禁止
- ❌ **項目85**: 自立した新規話題・質問における直前会話の引きずり（Topic Carryover）および短文一律FOLLOW誤判定・記憶検索強制合成の禁止（トピック・アイソレーションの徹底）
- ❌ **項目86**: 外部調査プロンプト対話相手欠落・Bot自己メンション出力・文脈言及メンバー誤爆の禁止（対話相手明示・自己メンションサニタイズ・照応言及ガードの多重防御）
- ❌ **項目87**: 長期記憶抽出における感情理由・別文脈からの事実捏造（ハルシネーション）の禁止（プロンプト感情理由撤廃・捏造指示明記・会話根拠ガードの多重防御）
- ❌ **項目88**: ユーザーの知識教示・定義文に対するWeb検索誤爆、話題転換（BREAK）時の長期記憶想起蒸発、および小型LLMによる無関係な記憶全選択（記憶誤爆）の禁止
- ❌ **項目95**: ティア表ゲームお題生成におけるAI記憶データベース参照の欠落・固定化の禁止（確率抽選・未ヒット時通常AIフォールバック・参加プレイヤー記憶優先の多重防御）
- ❌ **項目103**: 長期記憶抽出における無限ループ（Mode Collapse）・JSONトークン枯渇破損の防止とテストスタブ汚染の根絶
- ❌ **項目104**: サーバーメンバーUser-First照合のDB単独依存・未チャットメンバー見落としおよび話題転換（BREAK）時想起記憶脱落・不知シラ切り表明の禁止
- ❌ **項目106**: 記憶DB・事前分析におけるメタ記憶による実態記憶（エピソード）選外脱落の禁止、エピソード記憶からのメンバー解決（Entity Resolution）欠落の禁止、および包括的不知・シラ切りサニタイズ（多重防御）
- ❌ **項目125**: 定期内省（Reflection）におけるユーザー紐付け・抽象主語「ユーザーは」の正規化および蓄積パーソナリティの返信反映
- ❌ **項目126**: 長期記憶抽出（reason/memories）におけるサーバーニックネーム・ユーザーID紐付け＆事後正規化、および返信時におけるアカウント名呼称排除とサーバーニックネーム多重防御
- ❌ **項目127**: サーバーニックネーム変更（改名）時における記憶・信念・関係値の見失い防止、内部ユーザーID紐付け管理、および最新サーバーニックネーム動的置換・レンダリング
- ❌ **項目129**: 会話習慣プロファイル（update_user_habit_profile/reason）におけるサーバーニックネーム・ユーザーID紐付け＆事後正規化の欠落禁止
- ❌ **項目130**: 定期内省（Reflection）における休眠・未対話メンバーの個別選定誤爆防止（アクティブユーザー限定）、チャンネル全体内省での話者ラベル欠落・第三者名推測捏造禁止、および会話抽出時の対話相手と第三者・スレ名の主客分離
- ❌ **項目131**: フェーズ1（コンテキスト構築）における直前会話ログ行（Assistant:, System: 等）の長期記憶混入、事前分析LLMによるAI直前台詞誤選定、および重複ガード（フェーズ4）拒絶・フォールバック転落の禁止（多重防御・記憶サニタイズ）

### ⚡ 4. Discord API・プロセス管理・コマンド同期
- ❌ **項目7**: ニックネーム同期等における `GUILD_ID` 単一設定値依存の禁止（`bot.guilds` へフォールバック）
- ❌ **項目9**: 同一Botトークンによる複数プロセス稼働および個別 `tree.sync()` 実行の禁止
- ❌ **項目10**: 1ギルドあたり 6個以上の Message Context Menu 登録の禁止（最大5個）
- ❌ **項目11**: `setup_hook()` 内での長時間のブロッキング処理の禁止（重い初期化は `on_ready` 以降）
- ❌ **項目12**: コマンド同期におけるスコープ不整合・無理な `copy_global_to` の禁止（ハイブリッド同期を徹底）
- ❌ **項目36**: Discord Interaction（Modal等）の事前応答チェック欠落・未定義super().on_interaction呼び出し・承認済み例外（HTTP 40060）未捕捉および複数プロセス競合によるクラッシュの禁止
- ❌ **項目70**: スラッシュコマンドにおけるスコープ同期不整合（グローバル残存による他Bot汚染）、引数型アノテーション不整合（User vs Member）、および Interaction 応答例外未防護の禁止
- ❌ **項目78**: Bot停止要求に対するプロセス終了（exit）による再起動不能化、管理者権限制限の欠落、および停止中メッセージ応答・自発発言・他インタラクション漏れの禁止
- ❌ **項目79**: サブチャンネル自発応答におけるクールダウン（不応期）欠落による連続返信、およびメンション応答との競合・巻き戻しの禁止
- ❌ **項目80**: discord.py イベントハンドラ（`on_interaction` 等）における非存在 `super().on_xxx()` 呼び出しによる `AttributeError` 例外クラッシュの禁止
- ❌ **項目83**: IGNORE_USER_IDS 指定ユーザーに対するコマンド・インタラクション実行制限の欠落（チャットのみ除外・コマンドすり抜け）の禁止、およびエフェメラル通知とオートコンプリート保護
- ❌ **項目97**: 特定ユーザーのコマンド実行制限（COMMAND_IGNORE_USER_IDS）における通常会話保全と多重防御（エフェメラル通知・テキストコマンド遮断・通常チャット完全保全）
- ❌ **項目107**: 同一ギルド内マルチBot稼働における他Bot発言（author.bot=True）・担当外チャンネル雑談の最上流遮断（Early Drop）欠落によるログ混信・汚染の禁止、および無差別INFOログ出力の禁止
- ❌ **項目112**: マルチBot並行稼働における記憶DB（SQLite）・ゲームプール・感情/関係値・投稿履歴のコンフィグ単位自動分離（Auto-Isolation）欠落によるデータ混信・共有パス衝突の禁止、および主力Bot既存データ後方互換保全の徹底
- ❌ **項目116**: サブチャンネル応答頻度設定における最大間隔（確定返信）・クールダウン連動欠落による長期沈黙放置の禁止、および個別コンフィグ値優先解決と抽選可観測性の保証

### 🛡️ 5. 外部API・入力正規化・型安全性
- ❌ **項目2**: `MessageRuntime` 等の定義済みデータクラスへの勝手な属性追加の禁止
- ❌ **項目3**: 未確認プロパティへの直接アクセス禁止（`getattr` や型定義の事前確認）
- ❌ **項目4**: 既存非同期キュー等と並行する重複ロジックの追加禁止
- ❌ **項目13**: 外部API通信における包括的例外捕捉（`try...except`）漏れ・クラッシュ放置の禁止
- ❌ **項目14**: ユーザー入力パラメータ抽出時のメンション・接続詞・日付表現・助詞のトリミング漏れ禁止
- ❌ **項目29**: 共通アグリゲーター（`ollama_helpers` 等）における新設関数・型の再エクスポート漏れおよびインポート整合性破壊の禁止
- ❌ **項目37**: 動的型付け・型アノテーション欠落の禁止および mypy 静的型検査（0 errors）の厳格遵守
- ❌ **項目38**: 共通LLM関数の引数名・シグネチャ不整合（`prompt=` 誤用等）の禁止および外部依存未導入時の多重フォールバック徹底
- ❌ **項目60**: 共通基盤（`lib/`）をバイパスした設定パース・タイムゾーン（JST）重複再定義、requirements.txt未定義外部ライブラリ（pytz等）の依存残骸放置、および移行時後方互換アグリゲーター破壊の禁止
- ❌ **項目69**: スクリプト共通層への特定固有人格（コイトフクマル・シャンクス等）のハードコード、後方互換・テストを名目とした persona 分岐の残存、およびコンフィグ駆動設計バイパスの禁止
- ❌ **項目71**: 関数・メソッド内での重複ローカルインポートによる先行参照スコープ破壊（UnboundLocalError）の禁止
- ❌ **項目73**: サブチャンネル応答パラメータ（`OTHER_CHANNEL_RANDOM_MIN/MAX`）のカウンター方式化（蓄積件数閾値による完全沈黙）およびリセット巻き戻しループの禁止、並びに `OTHER_CHANNEL_IDS` 判定における素の `cfg()` 参照（型不一致によるチャンネル無視）の禁止
- ❌ **項目76**: 個別Botコンフィグの起動引数解決漏れ、共通環境変数による個別デフォルト値上書き、および設定名正規化不備の禁止
- ❌ **項目81**: ローカル開発環境と本番VPS同期境界の混同（サーバー直編集コマンド押し付け）の禁止、およびログのキー命名不一致（reason 誤用）による primary チャンネル無視誤認の防止
- ❌ **項目82**: リリース用リポジトリ／公開フォルダへの修正同期漏れ（リリース同期ギャップ）の禁止、およびマルチリポジトリ横断検証の徹底
- ❌ **項目98**: ハーネス設計移行に伴う本番デプロイ対象外漏れ・循環インポート・非推奨警告の禁止（allowed_items同期・sys.path多重解決・遅延インポート・catch_warnings）
- ❌ **項目100**: ハーネス設計に基づくプロンプトマネージャー＆パーサーの完全集約と分散残骸の禁止（harness/agent/prompt_parser一元管理・シンボリックリンク排除・[prompt-manager]ログ徹底・50KBルール遵守）
- ❌ **項目101**: プロンプト受付からRAG・事前分析・プロンプト結合・サニタイズに至る8段階パイプラインのバイパスおよび多重防御欠落の禁止
- ❌ **項目105**: ハーネス設計に基づくツールレジストリ＆Action実行エンジンの分散実装・直接呼び出しの禁止（harness/agent/tool_registry一元管理・[tool-registry]ログ徹底・引数/タイムアウト多重防御・50KBルール遵守）
- ❌ **項目108**: 気象情報処理における旧Open-Meteo API依存・旧weather_helpers直接参照の放置および国内気象MCP（JMA・ウェザーニュース）移行不徹底の禁止
- ❌ **項目110**: ハーネス設計移行における旧直接LLM呼び出しフォールバック放置・旧モジュール依存残存の禁止（自律エージェントフロー一本化・旧フォワーダー完全撤廃・多重防御）
- ❌ **項目111**: ハーネス設計各フェーズ（Context/Thought/Action/Observation）の責務逸脱、旧パスへの逆参照残存・フォワーダー依存放置による型不整合、およびツール結果観察・評価（Observation）バイパスの禁止（旧フォワーダー完全撤廃・直接参照一本化）
- ❌ **項目113**: ファクトチェック処理における旧直接スクリプト直接参照・非MCP化放置の禁止、およびMCPサーバー（FactCheckMCPServer）への完全集約・直接参照一本化（旧フォワーダー完全撤廃）
- ❌ **項目117**: ハーネス設計ReAct5分類（Context/Thought/Action/Observation/Orchestrator）の構造逸脱・旧パス二重化・sys.modules同期漏れの禁止（直接参照一本化・正本配置・エイリアス同期・動的cfg解決）
- ❌ **項目120**: 旧記憶レイヤー（ollama_bot/common/memory*）の再導入・残骸放置および不要な後方互換レイヤー放置の禁止（Memory Manager ハーネス集約と完全移行・動的委譲ファサード・MagicMock await 多重防御・ダミー文言重複防止）
- ❌ **項目121**: Agentic Loopにおけるフェーズ間メモリデータフロー（Read/Write/Save）の断絶、Observationルーターの軽微ループ警告（ALTERNATIVE_TOOL）によるFATAL_ERROR誤爆、および致命的エラー中断時LLM最終推論リークの禁止（動的RAG想起・_save_memory完全実装・ReAct再試行保護・履歴トリミング・安全停止ガード）

### 🤖 6. AIの内部状態・推論モデルの暴走防止
- ❌ **項目30**: 感情ヒューリスティックにおける「相手の顔文字」誤認・喜び持続による話題すり替え（マジカルバナナ）の禁止
- ❌ **項目31**: 文脈判定器（Context Checker）の道徳判定暴走（セーフティ指示）による強制的な文脈切断の禁止
- ❌ **項目32**: 推論モデル（Qwen等）における `think: false` 無視と `num_predict` 枯渇による空文字フォールバックの禁止
- ❌ **項目33**: 過去記憶のテキスト注入に釣られた設定外の一人称（私）ブレの禁止
- ❌ **項目94**: ティア表パーティゲームにおける選択肢（options）の数字・記号のみ出力および未検証採用の禁止（具体例提示・多重バリデーション・リトライ機構）
- ❌ **項目96**: ティア表パーティゲームにおける特定事例特化の排除と普遍的品質保証アーキテクチャ（外国語・架空造語・音韻規則・客観事実クイズ・同語反復の多重防御）
- ❌ **項目118**: 推論・計画立案における自己発言の前言撤回・未体験者ぶり自己矛盾、ロール反転、および語彙すり替えハルシネーションの禁止
- ❌ **項目119**: 意図解釈・調査計画立案（Thought）におけるHow-to/SNS話題混同、ルールによるモデル推論上書き、およびリプライ時新規質問のメタ解説（simplify）誤爆の禁止
