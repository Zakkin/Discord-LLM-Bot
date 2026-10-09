# 🧠 Memory Manager 開発・運用ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/memory_manager`）は、Discord AI Bot / AIエージェント・ハーネスアーキテクチャにおける **「1-3. メモリマネージャー (記憶・学習・プロファイル・内省基盤)」** を担当する独立統合パッケージです。

本パッケージで作業を行うすべてのAIエージェントおよび開発者は、以下の**設計思想**、**アーキテクチャ構成**、**データベース仕様**、**品質・安全性ガード（多重防御）**、および**コーディング規約**を必ず参照・遵守してください。

---

## 🏛️ アーキテクチャと階層的位置づけ

### 1. 全体ハーネスにおける位置づけ
メモリマネージャーは、AIエージェントがユーザーと長期的に自然で一貫した対話を行い、関係性を深めるための頭脳・記憶中枢です。

```text
AIエージェント・ハーネス（全体システム）
  1. 実行基盤（エージェント本体）
    1-1. プロンプトマネージャー & パーサー  [harness/agent/prompt_parser]
    1-2. オーケストレーター (フェーズ制御)   [harness/agent/orchestrator]
    1-3. メモリマネージャー (フェーズ1/4/5)   [★本パッケージ: harness/agent/memory_manager]
       ├── DBストレージ基盤（SQLite / sqlite-vec / FTS5）
       ├── 長期記憶・ベクトル検索（RAG / Hybrid Search）
       ├── インタラクション・会話履歴・短期バッファ管理
       ├── ユーザープロファイル・習慣・関心事管理
       ├── 記憶抽出・ハルシネーション遮断ガード
       └── 定期内省（Reflection）プロンプト構築 & 信念バリデーション
    1-4. ツールレジストリ                   [harness/agent/tool_registry]
    1-5. 結果の観察と評価                   [harness/agent/observation]
  2. テストハーネス                         [harness/agent/test_harness]
```

### 2. パッケージ内ディレクトリ構成
すべてのソースコードは **50KB ルール（項目100）** を遵守し、責務ごとに明確に分離されています。

```text
harness/agent/memory_manager/
├── __init__.py                # 公開APIエクスポート（MemoryStore, extractor, retriever等）
├── AGENTS.md                  # 本ガイドライン（開発原則・禁止事項・設計仕様）
├── GEMINI.md                  # AIエージェント向けクイックリファレンス
├── README.md                  # パッケージ概要ドキュメント
├── types.py                   # 記憶データ型・TypedDict・Protocol定義
├── compat.py                  # 高速JSONシリアライザ（orjsonフォールバック）・ISO時刻・動的ヘルパー解決
├── logic.py                   # 互換性維持および統合ファサード
├── store.py                   # Storage Mixinを統合した MemoryStore ファサード
│
├── storage/                   # DB物理ストレージ・テーブル操作層
│   ├── __init__.py            # Storageレイヤー公開
│   ├── base.py                # コネクションリース、WAL/PRAGMA設定、sqlite-vecロード、DDL、FTS初期化
│   ├── core_store.py          # 長期記憶CRUD、ベクトル類似度検索、FTS5全文検索、ハイブリッド検索、減衰
│   ├── interactions.py        # Bot発話ログ、チャンネル要約、スレッド会話履歴、エピソード記憶、展望記憶
│   ├── profiles.py            # ユーザー関心事（時間減衰付き）、Discordプロファイル、関係性ログ
│   └── learning.py            # 習慣プロファイル、学習候補データ、フィードバック、内省信念
│
├── extractor.py               # 記憶抽出（extract_and_store_memory）& ハルシネーション多重防御
├── retriever.py               # 記憶想起（get_relevant_memories / RAG）、トピック隔離、感情フック
├── summarizer.py              # 習慣プロファイル（EMA更新）、チャンネル要約、学習候補自動保存
├── reflection.py              # 定期内省プロンプト生成、信念バリデーション、第三者誤認防止
└── tests/                     # パッケージ内単体テスト群
    ├── conftest.py            # テスト環境初期化
    └── test_memory_manager.py # ライフサイクル・ガード・エクスポート検証テスト
```

---

## 🗄️ データベース設計とテーブル仕様

本パッケージは非同期 SQLite（`aiosqlite`）をベースとし、`sqlite-vec`（ベクトル類似度検索）および `FTS5`（全文検索）を併用するハイブリッドストレージです。

### 1. 接続管理とパフォーマンス設定 (`storage/base.py`)
- **コネクションリース (`_ConnectionLease`)**: 単一のコネクションを効率的に再利用しつつ、既存の `_cursor(commit=True)` 呼び出しを安全に処理。
- **PRAGMAチューニング**:
  - `PRAGMA journal_mode=WAL;`（読み書きの並行性確保）
  - `PRAGMA synchronous=NORMAL;`（安全性を保ちつつI/O高速化）
  - `PRAGMA foreign_keys=ON;`
  - `PRAGMA busy_timeout=30000;`（ロック競合時の待機）
  - `PRAGMA cache_size=-64000;`（64MBキャッシュ）
  - `PRAGMA mmap_size=268435456;`（256MBメモリマップ）

### 2. テーブル構成一覧
1. **`memories`** / **`vec_memories`** / **`fts_memories`**:
   - 長期記憶本体テーブル、ベクトル検索用仮想テーブル（`vec0`）、全文検索用仮想テーブル（`fts5`）。
   - **完全同期ルール（項目106）**: `add_memory` / `delete_memory` 時には、これら3つのテーブルに対して必ず同一トランザクション内で整合性を維持して更新・削除を行う。
2. **`bot_utterances`**: Bot自身の直近発話ログ（オウム返し防止・文脈参照用）。
3. **`channel_summaries`**: チャンネルごとの長期会話要約。
4. **`channel_threads`**: スレッド・会話ターンごとのトピックと参加者履歴。
5. **`memory_episodes`**: 単発の出来事・エピソード記憶（出来事、Bot行動、ユーザー反応、感情推移）。
6. **`memory_prospective`**: 展望記憶（将来の特定条件・キーワードで発火するトリガー型プロンプト）。
7. **`user_habit_profiles`**: ユーザーごとの会話習慣プロファイル（`tone_casual`, `response_detail`, `search_preference`, `joke_receptivity`）。EMA（指数移動平均）で逐次更新。
8. **`user_interests`**: ユーザーの関心事・話題・キーワード。言及頻度と時間減衰（Decay）により自動更新・パージ。
9. **`user_profiles`**: Discordサーバーメンバーの表示名、別名（aliases）、ロール履歴。
10. **`user_relationship_logs`**: 会話ごとの感情・親愛・信頼・好感度の推移ログ。
11. **`training_candidates`**: Fine-tuningや強化学習用の対話ペア候補データ。
12. **`feedback_items`**: ユーザーからの修正指示・フィードバック履歴。
13. **`reflections`**: 定期内省により抽出されたエージェント／ユーザー／チャンネルスコープの信念（Belief）・文化。

---

## 🛡️ 品質基準と多重防御ガード (Core Rules & Bans)

本パッケージの実装および修正においては、以下の禁止事項・保護ルールを厳格に順守してください。

### 1. 長期記憶抽出における事実捏造・感情理由混入の完全遮断（項目87）
- **感情理由の引き写し遮断 (`_is_hallucinated_emotion_reason_memory`)**:
  直前の感情理由（`emotion_reason`）に含まれる内省的メモや困惑を、ユーザーが発言・共有した事実であるかのように記憶化することを禁止。
- **会話根拠のない架空事実の遮断 (`_is_unsupported_hallucinated_memory`)**:
  抽出された記憶の主要キーワードが、実際の会話（ユーザー発言、AI返答、直近文脈）に一切存在しない場合、保存前に自動破棄。
- **AI自身のキャラクター設定・返答方針の誤認保存遮断 (`_is_self_referential_memory`)**:
  AI自身（Botキャラクター名）の属性やプロファイル、あるいはAI側の返答ルール（「短い発言には戸惑う」等）をユーザー記憶（`behavioral_principle` など）として抽出・保存することを厳禁。
- **AI返答丸写しの防止 (`_is_assistant_quote_like`)**:
  AI自身のセリフそのものをユーザーの記憶として保存することを防止。
- **記憶タイプのホワイトリスト制限**:
  `ALLOWED_MEMORY_TYPES` に含まれる正規の型（`user_preference`, `preference`, `fact`, `setting` 等）のみ許可し、`BLOCKED_MEMORY_TYPES`（`user`, `ai`, `dialogue` 等）を完全遮断。

### 2. 記憶想起（RAG）におけるトピック隔離と感情フック（項目85）
- **自立話題の隔離 (`retriever.py`)**:
  ユーザーの疑問・挨拶・自立した質問に対し、直前AI発言のキーワードを記憶検索クエリに強制合成して過去の話題を引きずることを防止。
- **ハイブリッド検索 (`search_memories_hybrid`)**:
  埋め込みベクトル検索（Dense / `sqlite-vec`）と全文キーワード検索（Sparse / `FTS5`）を RRF（Reciprocal Rank Fusion）で統合し、取りこぼしとノイズを最小化。
- **感情フック照合**:
  現在の感情状態と記憶保存時の感情タグを照合し、現在の気分に呼応する記憶（`_emotion_hook_match`）を適切に優先順位付け。

### 3. 定期内省（Reflection）における第三者呼称誤認防止（項目93）
- **対象ユーザー名の明示**:
  内省プロンプト（`build_reflection_prompt`）に対象ユーザーの表示名を明示し、文脈に登場した第三者との混同を警告。
- **信念バリデーションガード (`is_reflection_belief_valid`)**:
  会話ログに登場した第三者名（別メンバー名等）を対象ユーザーの好む呼び名として登録する誤認や、根拠件数不足（`evidence_count < 2`）の単発あだ名の固定化を保存前に遮断。

### 4. ユーザー習慣プロファイルと関心事の動的更新
- **EMA（指数移動平均）更新 (`summarizer.py`)**:
  急激なプロファイル変動を防ぎ、十分な確信度（`confidence >= min_confidence`）がある場合のみ緩やかにスコアを更新。
- **時間減衰と自動パージ (`profiles.py`)**:
  古い関心事スコアを時間の経過とともに減衰させ、アクティブな話題のみを上位に保持。

### 5. Agentic Loop におけるメモリデータフロー（Read/Save）の完全性（項目121）
- **フェーズ1（コンテキスト構築・Read）の動的想起**:
  オーケストレーター実行時、渡された `MemoryStore` から `search_memories_hybrid`（ハイブリッド類似度検索）および `get_channel_summary`（チャンネル会話要約）を非同期に呼び出し、ユーザープロンプトに関連する長期記憶を動的注入する。
- **フェーズ5（タスク完了・Prune/Save）の定着保存**:
  ReActループ完了時、`_save_memory` は空スタブを排し、Bot発話ログ（`add_bot_utterance`）、確定事実（`add_memory`）、およびツール実行エピソード（`add_episode`）を長期記憶へアトミックに定着保存する。
- **安全停止時の推論・保存バイパス**:
  致命的エラー（`FATAL_ERROR`）による安全停止時は、LLMへの不要な最終推論および記憶保存を確実にバイパスし、エラー内容の汚染保存を防止する。

---

## 💻 開発・コーディング規約

1. **単一ファイル50KBルールの死守（項目100）**:
   - いかなるファイルも 50KB を超えてはならず、新機能の追加時は責務に応じたモジュール分割を実施すること。
2. **Python 3.11+ 型付けと厳格な静的型チェック**:
   - すべての関数・引数・戻り値に正確な型アノテーションを付与し、`mypy` エラー 0 件を維持すること。
   - 辞書構造には `TypedDict`（`types.py`）を徹底。
3. **包括的例外処理とフォールバック**:
   - `sqlite-vec` や `FTS5` の拡張ロード失敗時にも、Python側スコアリング等の代替手段でシステム全体が停止しない多重防御を維持すること。
4. **テストの実行**:
   - 修正後は必ず単体テストを実行し、全テストの通過を確認すること：
     ```bash
     PYTHONPATH="/Users/Shadow/Documents/05 開発/discord-ai-bot" python3 -m unittest discover -s tests
     ```
