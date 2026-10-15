# 🤖 Memory Manager サブプロジェクト開発ガイドライン (AGENTS.md)

本ディレクトリ（`harness/agent/memory_manager/`）は、AIエージェントの**メモリ管理（短期記憶・長期記憶/RAG・要約保存）**を担当するサブプロジェクトです。

---

## 🎯 担当範囲と責務
1. **永続化と検索基盤（RAG）**:
   - `MemoryStore` による SQLite / sqlite-vec / FTS5 ハイブリッド記憶データベース管理。
   - ベクトル類似度検索、全文検索、時間減衰スコアリング、関連記憶の取得（`retriever.py`）。
2. **記憶抽出・バリデーション**:
   - `extractor.py` による対話ログからの長期記憶（fact, preference, culture等）の抽出。
   - ハルシネーション（感情理由からの引き写し、架空エピソード、第三者混同）の保存前多重防御ガード。
3. **プロファイルと要約の更新**:
   - 習慣プロファイル（`update_user_habit_profile`）、ユーザー興味関心、チャンネル要約（`maybe_update_channel_summary`）。
   - ユーザー表示名解決（`user_identity_resolver.py`）。
4. **返信後メモリ永続化ハンドラー**:
   - `post_reply_handler.py` によるフェーズ5完了後の安全な長期記憶抽出、エピソード保存、スレッド永続化。

---

## 🚫 境界条件と禁止事項（最重要）
1. **【スコープ外ファイル編集の厳禁】**:
   - **自身が担当する `harness/agent/memory_manager/` 配下のファイルのみを編集すること。**
   - 他サブプロジェクト（`prompt_parser/`, `tool_registry/`, `observation/`, `orchestrator/`, `ollama_bot/`）のファイルを直接変更してはならない。
   - インターフェースや外部型の変更が必要な場合は、自己判断で書き換えず、ルートエージェント（トリアージ・アーキテクト）へエスカレーションすること。
2. **アトミック書き込みの遵守**:
   - データベースやJSONファイルの永続化において、一時ファイル `fsync` ➜ `replace` によるアトミック操作を行い、クラッシュによるデータ破損を絶対に防ぐこと。
3. **長期記憶抽出における事実捏造（ハルシネーション）の遮断**:
   - 直前の感情理由（`emotion_state.reason`）や推論思考内の仮定を「ユーザーとの約束・事実」として捏造保存してはならない。
   - 会話ソースに根拠のない主要キーワードを含む記憶を保存前ガード（`_is_unsupported_hallucinated_memory` 等）で確実に除外すること。
4. **ユーザー教示文における無関係記憶全選択の防止**:
   - ユーザーが「〜とは…です」と定義を教えている文面に対して、無関係な過去記憶を無差別に全選択して混入させてはならない。

---

## 🛠️ コーディング規約と検証手順
- **型安全性**: Python 3.11+ 型アノテーションを完全に付与し、`mypy` エラー0件を死守すること。
- **検証コマンド**:
  ```bash
  # 本サブプロジェクトの単体テスト
  PYTHONPATH=mock_libs:. pytest tests/test_memory_logic.py tests/test_memory_store.py tests/test_memory_four_steps_verification.py
  # 型検査
  PYTHONPATH=mock_libs:. mypy --config-file mypy.ini harness/agent/memory_manager
  ```
