"""
[AI Agent Summary]
AIツッコミの事実化によって誤抽出・誤保存された汚染記憶レコードおよび学習候補レコードを物理削除するクリーンアップスクリプト。
Cleanup script to purge polluted memory and training candidate records caused by AI commentary hallucination.
"""
from __future__ import annotations

import os
import sys

# 【標準ライブラリ types シャドーイング回避】
# Pythonスクリプトを直接実行した場合、sys.path[0]に当ディレクトリが設定され、
# 同ディレクトリの types.py が標準ライブラリ types をシャドーイングして
# argparse -> re -> enum -> types のインポート連鎖で ImportError: cannot import name 'GenericAlias' from 'types'
# が発生するのを防止する。
_script_dir = os.path.abspath(os.path.dirname(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _script_dir]

# 標準ライブラリ types を確実に先行ロード
import types  # noqa: F401

# プロジェクトルート（3階層上）を sys.path に追加
_repo_root = os.path.abspath(os.path.join(_script_dir, "../../.."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import argparse
import sqlite3
from typing import Sequence


def cleanup_database(db_path: str) -> tuple[int, int]:
    """指定されたSQLiteデータベースから汚染された記憶レコードと学習候補を削除する。"""
    if not os.path.exists(db_path):
        print(f"[-] DBファイルが存在しません: {db_path}")
        return 0, 0

    print(f"[*] データベースのクリーンアップを開始: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    deleted_memories = 0
    deleted_candidates = 0

    try:
        # 1. memories テーブルからの削除
        # 対象条件:
        # - content LIKE '%独自の解釈・主張を提示している%'
        # - content LIKE '%コビーがチビの姿を出した直後%'
        cur.execute("""
            SELECT id, user_id, content
            FROM memories
            WHERE content LIKE '%独自の解釈・主張を提示している%'
               OR content LIKE '%コビーがチビの姿を出した直後%'
        """)
        mem_rows = cur.fetchall()

        for mem_id, user_id, content in mem_rows:
            print(f"  [DEL memory] id={mem_id}, user_id={user_id}, content={content!r}")
            cur.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            deleted_memories += 1

            # fts_memories 仮想テーブルからの削除（存在する場合）
            try:
                cur.execute("DELETE FROM fts_memories WHERE rowid = ?", (mem_id,))
            except Exception:
                pass

            # memories_fts 仮想テーブル（旧名・エイリアス等互換）
            try:
                cur.execute("DELETE FROM memories_fts WHERE rowid = ?", (mem_id,))
            except Exception:
                pass

            # vec_memories 仮想テーブルからの削除（存在する場合）
            try:
                cur.execute("DELETE FROM vec_memories WHERE memory_id = ?", (mem_id,))
            except Exception:
                pass

        # 2. training_candidates テーブルからの削除
        # 対象条件: reason LIKE '%独自の解釈を主張したため%' または '%コビー%独自の解釈%'
        cur.execute("""
            SELECT id, user_id, reason
            FROM training_candidates
            WHERE reason LIKE '%独自の解釈を主張したため%'
               OR reason LIKE '%コビー%独自の解釈%'
        """)
        tc_rows = cur.fetchall()

        for tc_id, user_id, reason in tc_rows:
            print(f"  [DEL training_candidate] id={tc_id}, user_id={user_id}, reason={reason!r}")
            cur.execute("DELETE FROM training_candidates WHERE id = ?", (tc_id,))
            deleted_candidates += 1

        conn.commit()
        print(f"[+] クリーンアップ完了: {db_path} (削除件数: memories={deleted_memories}, training_candidates={deleted_candidates})")

        # 削除後の残存確認（0件チェック）
        cur.execute("SELECT count(*) FROM memories WHERE content LIKE '%独自の解釈・主張%' OR content LIKE '%コビーがチビの姿を出した直後%'")
        mem_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM training_candidates WHERE reason LIKE '%独自の解釈を主張したため%'")
        tc_count = cur.fetchone()[0]
        print(f"  [確認] 残存件数: memories={mem_count}, training_candidates={tc_count}")

    except sqlite3.OperationalError as e:
        print(f"[-] テーブルまたはDB操作エラー: {e}")
    finally:
        conn.close()

    return deleted_memories, deleted_candidates


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="汚染記憶レコードの物理削除クリーンアップスクリプト")
    parser.add_argument("db_paths", nargs="*", help="対象のSQLiteデータベースファイルパス")
    args = parser.parse_args(argv)

    target_paths: list[str] = []
    if args.db_paths:
        target_paths.extend(args.db_paths)
    else:
        # デフォルト候補パス
        env_path = os.environ.get("MEMORY_DB_PATH", "").strip()
        candidates = [
            env_path,
            "/var/lib/ollama-bot/memory_img.db",
            "/var/lib/ollama-bot/memory.db",
            "memory_img.db",
            "memory.db",
        ]
        target_paths.extend([p for p in candidates if p])

    # 重複排除
    unique_paths = list(dict.fromkeys(target_paths))

    total_mem = 0
    total_tc = 0
    processed_any = False

    for path in unique_paths:
        if os.path.exists(path):
            processed_any = True
            m, t = cleanup_database(path)
            total_mem += m
            total_tc += t
        else:
            print(f"[INFO] スキップ（ファイル未検出）: {path}")

    if not processed_any:
        print("\n[注意] 指定または探索されたパスにDBファイルが見つかりませんでした。")
        print("本番環境または対象DBのあるホストで以下のようにパスを指定して実行してください:")
        print("  python3 harness/agent/memory_manager/cleanup_polluted_memories.py /path/to/memory.db")
    else:
        print(f"\n[総計] 削除済み memories: {total_mem}件, training_candidates: {total_tc}件")


if __name__ == "__main__":
    main(sys.argv[1:])
