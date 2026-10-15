[← READMEに戻る](../README.md)

# 📁 システムアーキテクチャ & ディレクトリ構成

本Botは、単一の肥大化したスクリプトではなく、厳格な責務分離と多重防御を備えたレイヤードアーキテクチャで構成されています。すべてのモジュールは原則 **50KB 未満** を維持し、高い凝集度と保守性を実現しています。

---

## 📁 ディレクトリ構成

```text
discord-ai-bot/
├── docs/                             # 📚 詳細ドキュメント
│   ├── architecture.md               # システムアーキテクチャ & ディレクトリ構成
│   ├── models.md                     # LLM モデル構成 & 推奨スペック
│   ├── external_api.md               # 外部 LLM API 連携 (OpenAI / Gemini)
│   ├── discord_setup.md              # Discord Bot 設定 & 権限・チャンネル設定
│   └── chrome_mcp.md                 # Chrome MCP サーバー連携設定
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

---

## 🏛️ アーキテクチャと責務分離

| レイヤー / パッケージ | 役割と責務 | 主な構成モジュール |
| :--- | :--- | :--- |
| **`harness/agent/`**<br>(ReAct エージェント・ハーネス) | 自律的な推論・ツール実行・記憶想起・観察評価を統括するコアエージェント基盤。5段階フロー（Context ➔ Thought ➔ Action ➔ Observation ➔ Final Response）を具現化。 | `orchestrator/`, `prompt_parser/`, `tool_registry/`, `observation/`, `memory_manager/`, `test_harness/` |
| **`lib/`**<br>(共通ユーティリティ基盤) | プロジェクト全体で横断的に利用される低結合・高凝集なユーティリティ群。外部依存の抽象化、アトミック安全書き込み、正規化等を担保。 | `json_utils.py`, `config_utils.py`, `date_utils.py`, `text_utils.py`, `discord_utils.py`, `db_utils.py` 等 |
| **`ollama_bot/`**<br>(Bot アプリケーション層) | Discord API（discord.py）と接続し、メッセージ受信・ルーティング、感情シミュレーション、Presence 同期、コマンド制御を行うアプリケーション層。 | `main.py`, `ollama_chat.py`, `config_base.py`, `ollama_chat_/`, `common/` |
| **`tier_game/`**<br>(独立パーティゲーム Cog) | Discord スラッシュコマンド（`/tier_game`）で動作する心理・価値観当てパーティゲーム。Bot の通常対話から完全に分離された独立パッケージ。 | `cog.py`, `session.py`, `topics.py`, `scoring.py`, `renderer.py`, `views.py` |
| **`tests/`**<br>(テストスイート) | ハーネス、プロンプト、ツール、セーフティガード、感情、ゲーム等、全方位の挙動を検証する 900 件超の回帰・単体テスト群。 | `test_harness_*.py`, `test_memory_*.py`, `test_reply_*.py`, `test_tier_game.py` 等 |

---

## 🔄 ReAct 5段階内部処理フロー

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
