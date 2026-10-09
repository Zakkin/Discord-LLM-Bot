# 🤖 Discord AI Bot

ローカル LLM (Ollama / llama-server) をバックエンドに備えた、**日本語での自然な対話・キャラクター表現に特化**した高機能かつ自律的な Discord AI チャット Bot です。  
感情シミュレーション、長期的エピソード記憶、ユーザー関係値モデル、Chrome MCP による Web 調査、話題乗っ取り防止（Anti-Hijack）ツリー保護などを統合し、日本語コミュニティの複数人雑談でも滑らかで人間味のある会話を実現します。

---

## ✨ 主な機能

- 🇯🇵 **自然な日本語対話・キャラクター表現特化 (Japanese Persona & Conversation)**:
  - 日本語特有の細やかな相槌、ボケへのツッコミ、タメ口・敬語・ネットスラングなど多様なキャラクターペルソナ・口調に柔軟に対応。
  - 全角半角正規化や日本語形態素・境界判定による誤判定防止、オウム返し抑止など、日本語コミュニティでの複数人同時雑談に最適化された会話制御。
- 🎼 **自律型 ReAct ハーネス・アーキテクチャ (Agent Harness)**:
  - 5段階（① Context ➔ ② Thought ➔ ③ Action ➔ ④ Observation ➔ ⑤ Final Response）の自律推論・行動ループを統括。
  - 軽量分類モデル（Middle-LLM）による事前意図判定（Tool Routing Gate）により、日常雑談は高速直接応答し、外部ツール必要時のみ動的バインド。
- 🧠 **ローカル LLM & クラウド API 連携 (Ollama / llama-server / OpenAI / Gemini)**:
  - Qwen3.6 / Qwen3.5 などの推論（Reasoning）モデルにネイティブ対応。
  - 会話生成、中間タスク（思考選別、感情分析）、画像認識（Vision）、埋め込み（Embedding）のマルチモデル運用。
  - OpenAI API 互換モード（`/v1/chat/completions`）により、OpenAI (GPT-4o) や Google Gemini (Gemini 2.0 Flash) 等のクラウド API ともシームレスに連携。
  - Speculative Decoding (llama-server) による超高速推論に対応。
- 💖 **動的感情状態 & ユーザー関係値システム**:
  - 喜び・怒り・悲しみなどの感情パラメータが会話によって動的に変動。
  - ユーザーごとの親愛度・信頼度（関係値）と所感を自動学習・蓄積。
  - アトミック書き込みによる安全な永続化と、時間経過による感情減衰（Neutral回帰）。
- 📚 **ハイブリッド長期記憶システム (MemoryStore)**:
  - SQLite (FTS5 全文検索) + `sqlite-vec` (ベクトル検索) による高精度な記憶想起（RAG）。
  - 記憶DB検索 → Web自然調査 → 不知表明の3段階フォールバック。
  - 会話に根拠のない架空事実の捏造（ハルシネーション）や過去の感情理由の混入を保存前に自動遮断。
- 🛡️ **会話ツリー分離 & Anti-Hijack 機構**:
  - Discord 返信（Reply）時に親メッセージを最優先文脈として保護。
  - 並行する別ユーザーの雑談から目的語や話題が混入する「話題乗っ取り」を根本遮断。
  - 自然な相槌を保護しつつ、オウム返しや前発言丸写しを検知・抑止する多層ガード。
- 🌐 **Web 自然調査 & 国内気象情報 MCP (Model Context Protocol)**:
  - 会話中の疑問や評判の調査が必要な際、ヘッドレス/常駐 Chrome 経由で自然に調査を行い、会話の中で 1〜2 文のネット評判として回答。
  - 気象庁（JMA）およびウェザーニュースと連携し、国内の気象警報・公式予報・実況解説を MCP 経由で自然に取得・提供。
- ⏰ **自発発話（Proactive Behavior）**:
  - 会話が途切れた放置時間や退屈度（boredom）に応じて、過去の記憶やユーザー関心事を元に Bot が自発的に話題を振る機能。
- 🎮 **ティア表パーティゲーム（Tier List Party Game）**:
  - 格闘ゲーム等の「ティア表（S〜Dランク付け）」を題材にした心理・価値観当てパーティゲーム（`/tier_game`）。
  - AIが「カレーに入れるなら？」などの白熱するお題と5つの選択肢を出題。
  - 主役プレイヤーの秘密のティア付けを参加者が予想し、一致度でポイントを競い合う。
  - 各参加者が主役を一巡し、ゲーム終了時には全参加者間の「ベスト相性度診断（魂の双子 / 運命のライバル）」を発表。
  - 専用チャンネルID指定（`TIER_GAME_CHANNEL_ID`）と他チャンネルからの自動誘導に対応。

---

## 📁 ディレクトリ構成

```text
discord-ai-bot/
├── harness/                          # 🎼 自律型 ReAct エージェント・ハーネス基盤
│   └── agent/
│       ├── orchestrator/             # ⑤ Orchestrator: 5段階推論・行動ループ統括エンジン
│       │   ├── engine.py             # AgentOrchestrator（自律ライフサイクル制御）
│       │   ├── llm_client.py         # LLM クライアント抽象レイヤー
│       │   └── types.py              # オーケストレーション設定・結果型定義
│       ├── prompt_parser/            # ① Context & ② Thought: プロンプト構築と推論サニタイズ
│       │   ├── prompt_manager/       # ① Context: 動的プロンプト生成・ツリー文脈保護・トピック隔離
│       │   │   ├── builder.py        # ユーザープロンプト動的構築
│       │   │   ├── analyzer.py       # Middle-LLM 事前分析（意図・記憶選別・整合性監査）
│       │   │   ├── system_prompts.py # 人格・状況に応じた動的システムプロンプト生成
│       │   │   ├── budget.py         # トークン予算管理・文脈トリム
│       │   │   ├── thread_tracker.py # 返信ツリー文脈追跡（目的語ハイジャック遮断）
│       │   │   └── tool_context.py   # ツールスキーマ・MCP定義の動的注入
│       │   └── parser/               # ② Thought: 出力サニタイズ・構文解析・安全性検証
│       │       ├── cleaner.py        # 思考タグ(<think>)・制御行・エコー行のサニタイズ
│       │       ├── salvage.py        # 未完思考ブロックからのセリフ安全救出
│       │       ├── validation.py     # オウム返し・前発言丸写し・キャラ崩壊等の多層検証
│       │       ├── structured.py     # JSONスキーマ検証付き構造化LLM呼び出し
│       │       └── actions.py        # ReAct/JSON ツール呼び出しパース・ディスパッチ
│       ├── tool_registry/            # ③ Action: ツールカタログ・実行エンジン・外部連携
│       │   ├── registry.py           # ツールカタログ管理（ToolRegistry）
│       │   ├── executor.py           # Action 実行エンジン・非同期ルーティング・タイムアウト制御
│       │   ├── decorators.py         # @tool 宣言的デコレータ
│       │   ├── web_research/         # Web 自然調査・検索実行パイプライン
│       │   ├── adapters/             # 外部アダプター（Web検索、国内気象、MCP）
│       │   └── mcp/                  # MCP クライアント連携（Chrome MCP, 気象庁&ウェザーニュース）
│       ├── observation/              # ④ Observation: 結果の観察・評価・ループ検知
│       │   ├── observer.py           # 実行結果の精査・正規化・ToolResult 変換
│       │   ├── evaluator.py          # 目的達成度評価・再試行 (Retry) / 代替ツール判定
│       │   ├── loop_detector.py      # 同一引数・エラーループ検知・無限ループ遮断
│       │   ├── emotion_evaluator.py  # 感情観察・理由サニタイズ・ハルシネーション遮断
│       │   ├── feedback_observer.py  # ユーザーリアクション（絵文字等）の観察・評価
│       │   └── synthesizer.py        # コンテキスト合成・観察履歴要約
│       ├── memory_manager/           # 🧠 メモリマネージャー（SQLite / sqlite-vec / FTS5）
│       │   ├── storage/              # ハイブリッド物理ストレージ層
│       │   │   ├── base.py           # コネクションリース、DDL、sqlite-vec ロード
│       │   │   ├── core_store.py     # 長期記憶 CRUD、ベクトル類似度検索、全文検索
│       │   │   ├── interactions.py   # 会話ログ、発言履歴、短期バッファ
│       │   │   ├── profiles.py       # ユーザープロファイル・習慣・関心事・信念
│       │   │   └── learning.py       # 会話ペア学習・フィードバックデータ
│       │   ├── store.py              # MemoryStore 統合ファサード
│       │   ├── extractor.py          # 記憶抽出 & 架空事実・感情理由ハルシネーション遮断
│       │   ├── retriever.py          # 記憶想起 & トピック隔離
│       │   ├── summarizer.py         # ユーザー関心事・チャンネル要約更新
│       │   └── reflection.py         # 定期内省（Reflection）プロンプト構築 & 信念バリデーション
│       └── test_harness/             # 🛡️ テストハーネス（評価・安全性・監視基盤）
│           ├── sandbox/              # 隔離実行環境（ローカル権限制限、Docker、Wasm）
│           ├── telemetry/            # 分散トレーサー、メトリクス収集、JSONL 永続化
│           └── evaluator/            # 機械的アサーション、LLM-as-a-Judge、環境差分検証
├── lib/                              # 🛠️ 共通ユーティリティ基盤（横断的低結合モジュール）
│   ├── config_utils.py               # 設定値・環境変数パース・型安全ヘルパー
│   ├── date_utils.py                 # JST/UTC 日時・タイムスタンプ・フォーマット
│   ├── db_utils.py                   # データベース接続・トランザクションユーティリティ
│   ├── discord_utils.py              # Discord オブジェクト安全アクセス・テキスト正規化
│   ├── dispatch_utils.py             # 非同期タスクディスパッチユーティリティ
│   ├── html_utils.py                 # HTML パース・本文抽出
│   ├── json_utils.py                 # 高速 JSON 処理・アトミック安全書き込み
│   ├── media_utils.py                # 画像・メディアファイル処理
│   ├── text_utils.py                 # 全角半角正規化・URL抽出・文字数トリム
│   └── img2chan_utils.py             # 外部掲示板連携ユーティリティ
├── ollama_bot/                       # 🤖 Discord Bot アプリケーション層
│   ├── main.py                       # Bot 起動エントリポイント
│   ├── ollama_chat.py                # チャット機能 Cog エントリポイント・統合ファサード
│   ├── config_base.py                # 基本設定ベースクラス（全設定項目のデフォルト定義）
│   ├── config_loader.py              # 個別Bot設定動的ローダー
│   ├── config_sample.py              # キャラクター設定サンプル・テンプレート
│   ├── common/                       # Bot 固有共通ロジック
│   │   ├── bot_identity.py           # Bot 識別・他Bot判定・プレフィックス解決
│   │   ├── discord_helpers.py        # Discord API ヘルパー（送信、タイピング等）
│   │   ├── emotion_helpers.py        # 感情状態ヘルパー
│   │   └── ollama/                   # Ollama API クライアント・埋め込み・チャット
│   └── ollama_chat_/                 # チャット機能実装モジュール群
│       ├── agent/                    # 自発発話（Proactive）、内省、モノローグループ
│       ├── emotion/                  # 感情シミュレーション、関係値、Presence 同期
│       ├── events/                   # 画像・掲示板イベント、Web 調査
│       ├── lifecycle/                # 調査・モデル呼び出し・特殊アクション
│       └── ollama_chat_reply*.py     # 返答パイプライン（生成・文脈・メモリ）
├── tier_game/                        # 🎮 ティア表パーティゲーム機能パッケージ（独立 Cog）
│   ├── cog.py                        # スラッシュコマンド（/tier_game）・Cog 定義
│   ├── session.py                    # ゲームセッション状態管理
│   ├── models.py                     # データモデル
│   ├── topics.py                     # AI お題生成＆フォールバックプリセット
│   ├── scoring.py                    # スコア計算＆相性度診断
│   ├── renderer.py                   # Embed・ティア表テキスト描画
│   └── views.py                      # Discord UI（ボタン・エフェメラル対話）
├── modelfiles/                       # 🧠 Ollama Modelfile 定義
├── tests/                            # 🧪 統合テスト・単体テストスイート（900+ 件）
├── restart-ai.sh                     # 🚀 サービス管理・起動スクリプト（個別設定指定対応）
├── control_single_ollama.py          # 🔒 単一プロセス制御スクリプト
├── chrome_mcp_server.py              # 🌐 Chrome MCP サーバー起動スクリプト
├── mcp_chrome_connect_test.py        # 🔍 MCP 接続検証テストスクリプト
├── customize_for_github.py           # 📦 GitHub 公開・リリース用リポジトリ同期スクリプト
├── mypy.ini                          # 📋 静的型検査設定（Python 3.11+ / mypy 0 エラー運用）
├── requirements.txt                  # 📦 依存パッケージ定義
├── .env.example                      # ⚙️ 環境変数テンプレート
├── AGENTS.md                         # 🧭 開発・運用ガイドライン（正本・禁止事項全121項目）
└── GEMINI.md                         # 📖 AI エージェント向けガイドライン
```

### 🏛️ アーキテクチャと責務分離

本Botは、単一の肥大化したスクリプトではなく、厳格な責務分離と多重防御を備えたレイヤードアーキテクチャで構成されています。すべてのモジュールは原則 **50KB 未満** を維持し、高い凝集度と保守性を実現しています。

| レイヤー / パッケージ | 役割と責務 | 主な構成モジュール |
| :--- | :--- | :--- |
| **`harness/agent/`**<br>(ReAct エージェント・ハーネス) | 自律的な推論・ツール実行・記憶想起・観察評価を統括するコアエージェント基盤。5段階フロー（Context ➔ Thought ➔ Action ➔ Observation ➔ Final Response）を具現化。 | `orchestrator/`, `prompt_parser/`, `tool_registry/`, `observation/`, `memory_manager/`, `test_harness/` |
| **`lib/`**<br>(共通ユーティリティ基盤) | プロジェクト全体で横断的に利用される低結合・高凝集なユーティリティ群。外部依存の抽象化、アトミック安全書き込み、正規化等を担保。 | `json_utils.py`, `config_utils.py`, `date_utils.py`, `text_utils.py`, `discord_utils.py`, `db_utils.py` 等 |
| **`ollama_bot/`**<br>(Bot アプリケーション層) | Discord API（discord.py）と接続し、メッセージ受信・ルーティング、感情シミュレーション、Presence 同期、コマンド制御を行うアプリケーション層。 | `main.py`, `ollama_chat.py`, `config_base.py`, `ollama_chat_/`, `common/` |
| **`tier_game/`**<br>(独立パーティゲーム Cog) | Discord スラッシュコマンド（`/tier_game`）で動作する心理・価値観当てパーティゲーム。Bot の通常対話から完全に分離された独立パッケージ。 | `cog.py`, `session.py`, `topics.py`, `scoring.py`, `renderer.py`, `views.py` |
| **`tests/`**<br>(テストスイート) | ハーネス、プロンプト、ツール、セーフティガード、感情、ゲーム等、全方位の挙動を検証する 900 件超の回帰・単体テスト群。 | `test_harness_*.py`, `test_memory_*.py`, `test_reply_*.py`, `test_tier_game.py` 等 |

#### 🔄 ReAct 5段階内部処理フロー
```mermaid
flowchart TD
    UserPrompt["ユーザーからのメッセージ"] --> P1["1. コンテキスト構築 (Context)\n- 会話履歴 / 返信ツリー文脈\n- 長期記憶想起 (RAG / sqlite-vec)\n- ツールカタログ動的結合"]
    P1 --> LoopStart{"ループ制御 (Turn 1..N)"}
    LoopStart --> P2["2. 推論と計画立案 (Thought)\n- LLM推論 (Qwen系等)\n- 思考プロセス & Actionパース"]
    P2 -->|ツール不要| P5["5. 最終応答の生成 (Final Response)\n- サニタイズ・思考漏れ遮断\n- 発言とエピソードの長期記憶定着"]
    P2 -->|Function Calling| P3["3. ツール選択と実行 (Action)\n- ToolRegistry / ToolExecutor\n- Web調査 / 気象MCP / ゲーム処理"]
    P3 --> P4["4. 結果の観察と評価 (Observation)\n- 実行結果の精査 & サニタイズ\n- エラーループ検知 (LoopDetector)\n- 目的達成度評価 (ObservationEvaluator)"]
    P4 -->|情報十分 (GOAL_ACHIEVED)| P5
    P4 -->|再試行 / 別ツール (RETRY)| LoopStart
    P5 --> Output["ユーザーへのDiscord返答"]
```

---

## 🧠 LLM モデル構成と推奨スペック (Model Architecture & Hardware)

本 Bot は、単一の巨大モデルにすべての負荷を集中させるのではなく、役割ごとに特化した **3〜4 種類のモデルを連携運用** するマルチモデル・アーキテクチャを採用しています。

### 1. 用意すべきモデルの規模と個数（マルチモデル運用）

| 役割 | 推奨モデル例 | 推奨パラメータ規模 | 役割と動作タイミング |
| :--- | :--- | :---: | :--- |
| **① メイン会話モデル**<br>(Main Persona) | `Qwen3.6-35B-A3B`<br>`Qwen2.5-14B/32B`<br>`Qwen3.5-9B` | **8B〜35B**<br>(Q4_K_M 等) | Discord ユーザーとのメイン対話、ペルソナ口調の再現、ゲーム進行。思考モード（`<think>`）対応。常に VRAM 常駐（`keep_alive=-1`）推奨。 |
| **② 中間分類モデル**<br>(Middle-LLM) | `middle-llm`<br>(`Qwen3.5-9B`<br>`Qwen2.5-7B/3B`) | **3B〜9B**<br>(Q4) | 事前意図判定（Tool Routing Gate: 外部ツール/MCP要否）、記憶選別、感情分析（Scoring）、習慣抽出、構造化JSON抽出。高速・決定論的（`temp: 0`）。120秒間 VRAM キャッシュ（`keep_alive=120`）。 |
| **③ 埋め込みモデル**<br>(Embedding) | `bge-m3` | **約 560M**<br>(1024次元) | 長期記憶（MemoryStore）のベクトル類似度検索（RAG）。FTS5 全文検索とハイブリッド結合して記憶を高速想起。 |
| **④ 画像認識モデル**<br>(Vision / 任意) | `qwen2.5-vl`<br>`llava`, `minicpm-v` | **7B〜32B** | Discord に画像が添付された際の内容認識・画像会話。必要時のみオンデマンドでロード（`keep_alive=0`）。 |

> [!TIP]
> **モデル統合による VRAM 節約**:
> 以前は分類モデル（`classifier-local-v2`）と中間モデル（`middle-llm`）を別々にロードしていましたが、現在は両者を `middle-llm`（1つ）に統合する設計となっています。これによりモデル切り替えのロード時間を削減し、VRAM 消費を最小限に抑えています。

### 2. ハードウェア・GPU (VRAM) の目安
ローカル GPU で運用する場合の推奨スペックです。

- **16 GB VRAM (RTX 4080 / 4070 Ti Super / RTX 3080 16GB / T4 / V100 等)**:
  - メインモデル: 8B〜14B (Q4_K_M) または 35B (軽量MoE/A3B)
  - 中間モデル: 9B (Q4)
  - 埋め込み: BGE-M3
  - 戦略: メイン常駐 ＋ 中間モデル（120秒キャッシュ）の動的スワップ運用により、単一 GPU でも快適に稼働。
- **24 GB+ VRAM (RTX 3090 / 4090 / A5000 / A10G 等)**:
  - メインモデル: 14B〜32B (Q4_K_M / Q8)
  - 中間モデル: 9B (常時常駐可能)
  - 複数モデルを VRAM に常時展開でき、ロード待ちゼロの超低レイテンシ運用が可能。

---

## ☁️ 外部 LLM API 連携 (OpenAI / Google Gemini)

本 Bot はローカルの Ollama / llama-server だけでなく、**OpenAI API 互換モード (`OLLAMA_USE_OPENAI_API=true`)** を標準搭載しており、**OpenAI (GPT-4o, GPT-4o-mini)** や **Google Gemini (Gemini 2.0 Flash, Gemini 1.5 Pro)** 等のクラウド API と接続して運用できます。

ローカル GPU を持たないマシンで運用したい場合や、中間タスクをクラウドにオフロードしてローカルの GPU 負荷を下げたい場合に最適です。

### 1. OpenAI API への接続設定
OpenAI 公式 API または互換プロキシ（LiteLLM / One API / vLLM）を使用する場合：

`.env` 設定例:
```ini
# OpenAI API 互換モードを有効化
OLLAMA_USE_OPENAI_API=true

# API エンドポイント
OLLAMA_BASE_URL=https://api.openai.com

# 使用モデル名
OLLAMA_MODEL=gpt-4o
OLLAMA_MIDDLE_MODEL=gpt-4o-mini
OLLAMA_VISION_MODEL=gpt-4o
```
※ API キー認証が必要な場合は、LiteLLM などのローカルプロキシを介して `Authorization: Bearer <KEY>` を付与するか、リバースプロキシでヘッダーを注入して中継します。

### 2. Google Gemini API への接続設定
Google Gemini は Google AI Studio が提供する **OpenAI 互換エンドポイント**（または LiteLLM）を利用して接続できます。

`.env` 設定例:
```ini
# OpenAI 互換モードを有効化
OLLAMA_USE_OPENAI_API=true

# Google Gemini の OpenAI 互換エンドポイント
OLLAMA_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai

# 使用モデル名
OLLAMA_MODEL=gemini-2.0-flash
OLLAMA_MIDDLE_MODEL=gemini-2.0-flash
OLLAMA_VISION_MODEL=gemini-2.0-flash
```

### 3. ローカル ＋ クラウドのハイブリッド運用 (おすすめ)
- **メイン対話モデル**: ローカルの `Qwen3.6` や `llama-server`（Modelfile で独自のキャラクター性や口調を厳密に固定）
- **中間分類タスク (`middle-llm`)**: 外部 API（`Gemini 2.0 Flash` や `GPT-4o-mini` など、超高速・安価なクラウドモデル）

このハイブリッド構成により、ローカル GPU の VRAM をメイン対話モデル専用に 100% 割り当てつつ、高精度かつ高速な意図判定や感情シミュレーションを実現できます。

---

## ⚙️ Discord Bot 設定と必要な権限 (Setup & Permissions)

Discord AI Bot を正常に稼働させるためには、Discord Developer Portal でのインテント有効化、サーバー招待時の権限付与、およびチャンネルやロールの設定が必要です。

### 1. Privileged Gateway Intents の有効化（必須）
Discord Developer Portal の **[Applications] ＞ 該当Bot ＞ [Bot]** タブにある **Privileged Gateway Intents** で、以下の2項目を必ず **ON** にして **Save Changes** で保存してください。未設定の場合、起動時にエラーとなります。

| インテント名 | 必須度 | 利用目的 |
| :--- | :---: | :--- |
| **Message Content Intent** | **必須** | メッセージ本文の読み取り。OFF の場合、Bot宛てメンション以外の発言本文が空文字となりAIが会話できません。 |
| **Server Members Intent** | **必須** | サーバーメンバー一覧・プロファイル情報の取得。会話中のメンバー名解決（User-First照合）やニックネーム同期に必須です。 |

### 2. 必要な Discord 権限 (Bot Permissions)
サーバー招待リンクの生成時、またはサーバー内の Bot 用ロールに付与すべき権限一覧です。

| 権限名 (Permission) | 種別 | 利用目的・影響 |
| :--- | :---: | :--- |
| **View Channels** (`閲覧`) | **必須** | 動作対象チャンネルの閲覧。 |
| **Send Messages** (`送信`) | **必須** | メッセージへのAI応答、自発発言の送信。 |
| **Send Messages in Threads** | **必須** | スレッド内での会話・返信。 |
| **Read Message History** | **必須** | 会話文脈の取得、返信ツリー親メッセージの追跡（Anti-Hijack保護）、長期記憶の抽出。 |
| **Embed Links** | **推奨** | ティア表パーティゲーム（`/tier_game`）のEmbed表示、リッチカード返答。 |
| **Attach Files** | **推奨** | 画像認識（Vision）や生成画像の添付。 |
| **Add Reactions** | **推奨** | 会話への共感・感情リアクション（Good/Bad）、フィードバック観察。 |
| **Use External Emojis** | 任意 | 他サーバーのカスタム絵文字を使用したリアクション付与。 |
| **Change Nickname** | **推奨** | 感情パラメータ（喜怒哀楽）に応じたBot自身のニックネーム・表情タグの動的同期。 |

> [!IMPORTANT]
> **ロール階層（Role Hierarchy）の注意点**:
> Bot が自身のニックネームを変更（`Change Nickname`）するには、Discord サーバー設定の **[ロール]** 一覧において、**Botに付与されたロールが一般メンバーよりも上位** に配置されている必要があります。

### 3. サーバー招待リンクの生成 (OAuth2 URL Generator)
Developer Portal の **[OAuth2] ＞ [URL Generator]** で招待リンクを作成します。
1. **SCOPES**:
   - `bot`（現在の Discord 仕様ではスラッシュコマンド権限も自動付加されます）
2. **BOT PERMISSIONS**:
   - 上記表の各権限にチェックを入れます（テスト環境等では `Administrator` による一括許可も可能）。
3. 生成された URL をブラウザで開き、導入先サーバーを選択して認証・追加します。

### 4. チャンネル動作・応答モードの設定
本 Bot はチャンネルごとに柔軟な応答モードを設定できます（`.env` または `config_*.py` で指定）。

- 🏠 **メイン応答チャンネル (`OLLAMA_CHANNEL_ID`)**:
  - Bot 専用の会話チャンネル。メンション不要で、チャンネル内のすべての通常メッセージに対して 100% 応答します。
- 💬 **サブチャンネル (`OTHER_CHANNEL_IDS`)**:
  - 一般の雑談チャンネル。通常メッセージに対して独立確率（サイコロ方式: `OTHER_CHANNEL_RANDOM_MIN` 〜 `MAX`）で自発的に会話に混ざります。
  - クールダウン（`OTHER_CHANNEL_COOLDOWN_MESSAGES`）により短時間での連続返信を防止。
  - ※ メンション（`@Bot`）またはBotへの返信（Reply）が行われた場合は、確率抽選をバイパスして 100% 即時応答します。
- 🎮 **パーティゲーム専用チャンネル (`TIER_GAME_CHANNEL_ID`)**:
  - ティア表パーティゲーム（`/tier_game`）を行う専用チャンネル。他チャンネルでコマンドが実行された場合、このチャンネルへ自動誘導します（0で全チャンネル許可）。
- 🔒 **Bot稼働・停止制御コマンド (`BOT_ADMIN_USER_IDS`)**:
  - 管理者ユーザーのみが `/bot start`, `/bot stop`, `/bot status`（またはテキストコマンド `!bot`）でBotの稼働／スタンバイ状態を切り替えられます。
  - 停止中は Presence が「DND / 停止中（スタンバイ）」となり、メッセージ処理や自発発言を安全に休止します。
- 🚫 **除外ユーザー (`IGNORE_USER_IDS`)**:
  - 指定したユーザーID（他Botや荒らし等）からの全インタラクション（チャット、スラッシュコマンド、ボタン等）を完全に遮断します。

---

## 🚀 クイックスタート

### 1. 必要要件
- **Python**: 3.11 以上 (3.11 〜 3.14 対応)
- **LLM 推論環境**:
  - **ローカル推論**: **Ollama** または **llama-server**（GPU 環境推奨）
  - **クラウド API**: **OpenAI API** (GPT-4o 等) または **Google Gemini API** (Gemini 2.0 Flash 等)
- **Discord Bot Token**: Discord Developer Portal から取得
- **Google Chrome / Chromium & Node.js** (任意): Chrome MCP による Web 自然調査機能を利用する場合

### 2. インストール
```bash
# リポジトリのクローン
git clone https://github.com/your-username/discord-ai-bot.git
cd discord-ai-bot

# 仮想環境の作成と有効化
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存パッケージのインストール
pip install -r requirements.txt
```

### 3. 環境設定 (.env)
`.env.example` をコピーして `.env` を作成し、必要なトークンや設定値を記入します。

```bash
cp .env.example .env
```

主な設定項目:
```ini
# Discord Bot トークン
DISCORD_TOKEN=your_bot_token_here

# 稼働させる Discord サーバー (Guild) ID
DISCORD_GUILD_ID=123456789012345678

# メイン応答チャンネル ID (全発言に応答)
OLLAMA_CHANNEL_ID=123456789012345678

# サブチャンネル ID (カンマ区切りで複数指定可・確率応答)
OTHER_CHANNEL_IDS=123456789012345679,123456789012345680

# Bot 管理者ユーザー ID (カンマ区切り・稼働制御用)
BOT_ADMIN_USER_IDS=123456789012345678

# --- 推論バックエンド設定 ---
# A. ローカル Ollama 運用の例:
OLLAMA_BASE_URL=http://127.0.0.1:11434

# B. クラウド API (OpenAI / Gemini) 運用の例:
# OLLAMA_USE_OPENAI_API=true
# OLLAMA_BASE_URL=https://api.openai.com
# OLLAMA_MODEL=gpt-4o
# OLLAMA_MIDDLE_MODEL=gpt-4o-mini
```

### 4. キャラクター設定ファイルの作成
`config_sample.py` をコピーして、あなた専用のキャラクター設定ファイル（例: `config_mybot.py`）を作成します。

```bash
cp ollama_bot/config_sample.py ollama_bot/config_mybot.py
```
`config_mybot.py` を開き、Botの名前、一人称、口調、コアバリュー等を自由にカスタマイズしてください。

### 5. Modelfile の作成 (Ollama)
`modelfiles/` ディレクトリに用意されている Modelfile からモデルを作成します。

```bash
ollama create middle-llm -f modelfiles/middle-llm.Modelfile
ollama create classifier-local-v2 -f modelfiles/classifier-v2.Modelfile
```

### 6. Bot の起動

#### スクリプトから起動する場合
```bash
./restart-ai.sh
```

#### 直接 Python で起動する場合
```bash
export OLLAMA_BOT_CONFIG=config_mybot
python3 -m ollama_bot.main
```

---

## 🌐 Chrome MCP サーバーの連携設定 (Web 自然調査)

本 Bot は **Chrome MCP (Model Context Protocol) サーバー** と連携することで、会話中の疑問や最新のネット評判、X (Twitter) タイムラインなどをブラウザ経由で自動調査できます。

### 📋 動作条件

1. **MCP HTTP エンドポイントの疎通**:
   - デフォルト: `http://127.0.0.1:12306/mcp`
   - Chrome 拡張機能およびネイティブホストが起動し、Streamable HTTP (MCP プロトコルバージョン `2025-03-26`) を受け付けていること。
2. **必須 MCP ツールの提供**:
   - Web 閲覧・解析に必要な以下のツールが MCP サーバーから正常に提供されていること:
     - `chrome_navigate`: 指定 URL への移動
     - `chrome_get_web_content`: ページ本文・DOM コンテンツの抽出
     - （任意: `chrome_read_page`, `chrome_close_tabs`, `chrome_get_page_metadata`, `chrome_screenshot` 等）
   - ※ 安全性のため、入力・送信・購入操作（`click`, `type`, `submit`, `buy` 等）のツールは自動的にブロック・除外されます。
3. **ディスプレイ環境 (Linux サーバーの場合)**:
   - Chrome を起動するための X11 / Wayland / Xvfb 環境（`DISPLAY=:0` など）が利用可能であること。
4. **セッション永続性と単一接続**:
   - MCP サーバーはトランスポート接続を維持するため、セッションファイル（`/tmp/discord-ai-bot-chrome-mcp-session`）を保持して再利用します。多重接続エラーが発生した場合は Chrome プロファイルの再起動が必要です。

### 🛠️ 準備すべきこと (セットアップ手順)

#### ① Google Chrome または Chromium のインストール
- **Linux (Ubuntu/Debian)**: `sudo apt install -y google-chrome-stable` または `chromium-browser`
- **Linux (Arch)**: `sudo pacman -S google-chrome` または `chromium`
- **macOS**: `Google Chrome.app` をインストール

#### ② `mcp-chrome-bridge` のインストールとマニフェスト登録
Chrome 拡張機能とネイティブホストを中継するブリッジツール（Node.js / npm）をインストールし、Native Messaging マニフェストを登録します。
```bash
# グローバルインストール
npm install -g mcp-chrome-bridge

# ネイティブホストマニフェストの登録
mcp-chrome-bridge register
```

#### ③ 専用 OS ユーザーと Chrome プロファイルの準備 (Linux 推奨)
Linux サーバーで root 運用する場合、Chrome は root 実行を制限するため、専用ユーザー（例: `mcpchrome`）を用意することを推奨します。
```bash
# 専用ユーザーの作成 (Linux)
sudo useradd -m -s /bin/bash mcpchrome

# 専用ユーザーで bridge を登録する場合
sudo -H -u mcpchrome npm install -g mcp-chrome-bridge
sudo -H -u mcpchrome mcp-chrome-bridge register
```

#### ④ Chrome 拡張機能（mcp-chrome-extension）の導入と初期接続
1. Bot 専用 Chrome プロファイルでブラウザを起動します（`./restart-ai.sh` を実行すると自動起動・構成されます）。
2. Chrome に `mcp-chrome` 拡張機能（未解凍拡張機能またはストア版）を読み込みます。
3. 拡張機能のアイコン/ポップアップを開き、**「Connect / Start」** をクリックしてネイティブサーバーを起動します。
4. ※ X (Twitter) などのログインが必要なサイトを調査対象にする場合は、この専用 Chrome プロファイル内で事前に一度だけ手動ログインを済ませておきます。

#### ⑤ 環境変数の設定 (`.env`)
`.env` に MCP サーバーの接続設定を追加・調整します。
```ini
# Chrome MCP サーバーのエンドポイント (デフォルト: http://127.0.0.1:12306/mcp)
WEB_RESEARCH_MCP_URL=http://127.0.0.1:12306/mcp
WEB_RESEARCH_MCP_TIMEOUT_SEC=20

# Linux サーバーで専用ユーザーを使う場合
MCP_CHROME_BOT_OS_USER=mcpchrome

# 未解凍拡張機能のパスを指定する場合 (省略時は ~/mcp-chrome-extension)
# MCP_CHROME_EXTENSION_PATH=/path/to/mcp-chrome-extension
```

#### ⑥ 接続テストの実行
付属の接続検証スクリプトを実行して、MCP サーバーおよび必須ツールが正常に応答するか確認します。
```bash
python3 mcp_chrome_connect_test.py
# 成功時の出力例: ok: initialized MCP session <session-id>; tools=9
```

---

## 📜 規約・ポリシー

- [利用規約 (Terms of Service)](TERMS_OF_SERVICE.md)
- [プライバシーポリシー (Privacy Policy)](PRIVACY_POLICY.md)

---

## 📄 ライセンス

このプロジェクトは [MIT License](LICENSE) のもとで公開されています。
