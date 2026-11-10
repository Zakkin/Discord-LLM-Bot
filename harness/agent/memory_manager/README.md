# 🧠 Memory Manager (`harness.agent.memory_manager` / `harness/agent/memory-manager`)

AIエージェント・ハーネスアーキテクチャにおける **「1-3. メモリマネージャー (記憶・学習・プロファイル・内省基盤)」** を担当する統合パッケージです。

---

## 📖 概要 (Overview)

本パッケージは、Discord AI Bot における **データベース基盤（SQLite / sqlite-vec / FTS5）** と、エージェントの短期・長期記憶（RAG）、会話エピソード、習慣プロファイル、ユーザー関心事、定期内省（Reflection）を一元管理します。

### 階層ツリー構造における位置づけ

```text
AIエージェント・ハーネス（全体システム）
  1. 実行基盤（エージェント本体）
    1-1. プロンプトマネージャー(フェーズ1)＆パーサー(フェーズ2)  [harness/agent/prompt_parser]
    1-2. オーケストレーター（フロー制御 フェーズ1/2/3/4/5を制御）  [harness/agent/orchestrator]
    1-3. メモリマネージャー(フェーズ1/4/5に関係)               [★本パッケージ: harness/agent/memory_manager]
       ├── DBストレージ基盤（SQLite / sqlite-vec / FTS5）
       ├── 長期記憶・ベクトル検索（RAG / Hybrid Search）
       ├── インタラクション・会話履歴・短期バッファ管理
       ├── ユーザープロファイル・習慣・関心事管理
       ├── 記憶抽出・ハルシネーション遮断ガード
       └── 定期内省（Reflection）プロンプト構築 & 信念バリデーション
    1-4. ツールレジストリ(フェーズ1/3に関係)                      [harness/agent/tool_registry]
    1-5. 結果の観察と評価(フェーズ4)                              [harness/agent/observation]
  2. テストハーネス（評価・安全性基盤）                           [harness/agent/test_harness]
```

---

## 📁 ディレクトリ構成 (Package Structure)

すべてのソースファイルは原則 **50KB 未満**（項目100）を維持し、責務ごとに分割されています。

```text
harness/agent/
├── memory_manager/                # [実体パッケージ] 1-3. メモリマネージャー
│   ├── __init__.py                # 公開APIエクスポート（MemoryStore, extract_and_store_memory等）
│   ├── README.md                  # 本ドキュメント
│   ├── types.py                   # 記憶データ型・辞書構造・プロトコル定義
│   ├── compat.py                  # 内部互換ユーティリティ（JSON, ISO時刻）
│   │
│   ├── storage/                   # DB・物理ストレージ層（SQLite / sqlite-vec / FTS5）
│   │   ├── __init__.py            # Storageレイヤー公開
│   │   ├── base.py                # コネクションリース、DDL、マイグレーション、sqlite-vecロード
│   │   ├── core_store.py          # 長期記憶CRUD、ベクトル類似度検索、全文検索
│   │   ├── interactions.py        # 会話ログ、発言履歴、短期バッファ
│   │   ├── profiles.py            # ユーザー・習慣・関心事・信念・エピソード・未来プロンプト
│   │   └── learning.py            # 学習データ・会話ペア・フィードバック
│   │
│   ├── store.py                   # 全Storage Mixinを統合した MemoryStore ファサード
│   │
│   ├── extractor.py               # 記憶抽出（extract_and_store_memory）& ハルシネーション多重防御
│   ├── retriever.py               # 記憶想起（retrieve_memories_for_prompt）& トピック隔離
│   ├── summarizer.py              # 習慣プロファイル・ユーザー関心事・チャンネル要約更新
│   ├── reflection.py              # 内省プロンプト生成 & 信念バリデーション
│   └── slang.py                   # BBSスラング辞書定義・SQLite永続化・自動Seed・照合・クエリサニタイズ
│
└── memory-manager -> memory_manager  # [シンボリックリンク] 指定パスへの完全互換エイリアス
```

---

## 🛡️ 多重防御と品質基準 (Quality & Safety Protocols)

本パッケージは `AGENTS.md` の設計原則および禁止事項（Ban List）を厳格に順守しています：

1. **長期記憶抽出における事実捏造・感情理由混入の完全遮断（項目87）**:
   - `_is_unsupported_hallucinated_memory`: 会話（ユーザー発言、AI返答、直近文脈）に根拠のないキーワードを含む架空の事実・約束を保存前に自動破棄。
   - `_is_hallucinated_emotion_reason_memory`: 過去の感情理由（`emotion_reason`）からの引き写し記憶を遮断。
2. **自立話題・質問におけるトピック隔離（項目85）**:
   - 自立した話題（`is_standalone_topic`）において直前AI発言の記憶クエリへの強制合成を遮断。
3. **定期内省における第三者呼称誤認防止（項目93）**:
   - `is_reflection_belief_valid` により、会話ログに登場した第三者名を相手の好む呼び名として学習することを防止。
4. **sqlite-vec と FTS5 の完全同期（項目106）**:
   - `add_memory` / `delete_memory` 時に基本テーブル `memories` と検索用仮想テーブル `vec_memories`, `fts_memories` の3重更新・削除を徹底。
5. **50KBルールの厳守（項目100）**:
   - 全てのソースファイルが 30KB 以下に収まるよう責務分割。
6. **返信プロンプトでのチャンネル要約取得のデフォルト有効化と文脈行数保護**:
   - `retriever.py` におけるチャンネル要約取得をデフォルト有効（`True`）とし、`post_reply_handler.py` における要約判定用文脈切り詰めを長文脈用（30行）に維持して要約未発動バグを防止。
7. **トピックラベルおよびキー・トピック永続化の接続**:
   - `ChannelSummaryRecord.topic_labels` および `MemoryEpisode.key_topics` の型定義と実データの整合性を保ち、会話トピックの追跡性を維持。
8. **記憶抽出モデルの軽量/中量優先化とGPU占有防止**:
   - `extractor.py` のモデル優先順位を `MEMORY_EXTRACT_MODEL` -> `OLLAMA_MIDDLE_MODEL` -> `OLLAMA_MODEL` とし、既定で軽量・中量モデルを使用。
9. **『ユーザー_<ID>』プレースホルダーの徹底排除と実名ニックネーム解決**:
   - `user_profiles` テーブルおよび `resolve_user_display_name` から実名表示名を解決し、本文中のプレースホルダーも `『{base_name}』さん` に置換。
10. **内省におけるチャンネル全体信念の抽象主語正規化**:
    - 特定個人に帰属できない内省信念の抽象主語（`ユーザーは/が` 等）を `"このチャンネルでは"` に正規化。
11. **BBSスラング・接頭辞誤認防止とSQLite永続化・検索クエリサニタイズ（項目47, 50）**:
    - `slang.py` のマスター辞書（20選以上）に基づき、「すみフラ」「うろフラ」等の掲示板接頭辞スラングを `filter_query_terms_by_slang` で検索クエリから除去し、「フラダンス」等の無関係な概念への誤検索や固有名詞誤読（知ったかぶり捏造）を防止。プロンプトへは `format_slang_guidance_for_prompt` により適切な注意書きを注入。
    - **SQLite DB (memory.db) 永続化と自動Seed基盤**:
      - `bbs_slangs` テーブルによる永続化、`seed_default_slangs` による安全な自動シード（`INSERT OR IGNORE`）、`upsert_slang` によるスラング動的追加・更新、`load_slangs_from_db` によるDB-キャッシュ同期を提供。
      - `MemoryStore` のインスタンスメソッドおよび `logic.py` ファサードから透過的に操作可能。
12. **深層反省プロンプトの一人称ルール記述の汎用化**:
    - `reflection.py`（`DEEP_REFLECTION_SYSTEM_PROMPT` および `build_deep_reflection_prompt`）における特定キャラクター名の直書きを排除し、設定された任意の一人称（例: 私は〜、自分は〜等）に準拠した汎用的な主語ルールを提供。
13. **prospective memory 期日管理とユーザーデータ物理削除・概要取得（プライバシー保護と自己修復）**:
    - `memory_prospective.due_at` による時間発火想起の保存・期日到来レコード取得（`list_due_prospective_memories`）。
    - ユーザーデータの完全物理削除 API（`delete_user_data`）: `memories`（および `vec_memories`, `fts_memories` の連動削除: BAN-183）、`memory_episodes`, `memory_prospective`, `user_habit_profiles`, `user_interests`, `reflections`, `user_relationship_logs`, `feedback_items`, `training_candidates` を1トランザクションで安全に物理削除。
    - ユーザー記憶開示 API（`get_user_memory_overview`）: ユーザーのプロファイル要約、直近の記憶一覧（id, text, created_at）、総件数を取得。
14. **会話状態メモ（短期・作業用）の永続化とチャンネル要約（長期用）の責務分離（BAN-143）**:
    - `conversation_states` テーブル新設により、ターンごとの作業前提（`topic`, `established_facts`, `denied_claims`, `open_questions`, `user_mood`）を保持し、長い会話での誤前提引きずられ破綻を防止。
    - `interactions.py`（`get_conversation_state`, `set_conversation_state`）により、非同期・安全な読み書きとメタキー処理を提供。チャンネル要約（`channel_summaries`）との責務分離を徹底。
