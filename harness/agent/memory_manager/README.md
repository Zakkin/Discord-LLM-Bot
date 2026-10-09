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
│   │
│   └── tests/                     # パッケージ内単体テスト
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
