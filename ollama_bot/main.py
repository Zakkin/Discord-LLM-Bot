"""互換エントリポイント: 実体は `front.discord.main` に移設されました。

`python3 -m ollama_bot.main` での起動と、旧パスからの import / mock.patch を維持します。
新規の起動は `python3 -m front.discord.main` を使用してください。
"""
import sys as _sys
from pathlib import Path as _Path

_repo_root = str(_Path(__file__).resolve().parent.parent)
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

try:
    import front.discord.main as _impl
    from front.discord.main import *  # noqa: F401,F403
except ModuleNotFoundError as _err:
    if _err.name == "front" or "No module named 'front'" in str(_err):
        _front_path = _Path(_repo_root) / "front"
        _sys.stderr.write(
            f"\n====================================================================\n"
            f"[FATAL ERROR] 'front' パッケージが見つかりません: {_front_path}\n"
            f"  - ディレクトリが存在するか?: {_front_path.is_dir()}\n"
            f"  - sys.path[0]: {_sys.path[0] if _sys.path else 'empty'}\n"
            f"  - 原因: 開発マシンで作成された `front/` フォルダがサーバに同期されていません。\n"
            f"  - 解決策: 開発マシンで `git add front/` してコミット＆プッシュし、\n"
            f"           サーバ側で `git pull` して `front/` を取得してください。\n"
            f"====================================================================\n\n"
        )
    raise

if __name__ == "__main__":
    _impl.main()
else:
    _sys.modules[__name__] = _impl
