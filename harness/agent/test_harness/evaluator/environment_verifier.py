"""AIエージェント・ハーネス 環境状態検証器 (environment_verifier.py)

2-3. エバリュエーター（自動評価器）として、
エージェントの行動（Action）によって指定されたファイルが作成されたか、
期待通りの内容が書き込まれたか等の環境状態変化を厳格に検証する。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Sequence

from .types import EnvironmentStateCheck

log = logging.getLogger("harness.agent.test_harness.evaluator.env")


class EnvironmentVerifier:
    """環境状態（ファイル・ディレクトリ等）の自動検証器"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir else None

    def _resolve(self, path: Path | str) -> Path:
        p = Path(path)
        if not p.is_absolute() and self.base_dir is not None:
            return (self.base_dir / p).resolve()
        return p.resolve()

    def verify_file_exists(self, target_path: Path | str) -> EnvironmentStateCheck:
        """指定ファイルが存在するかチェック"""
        resolved = self._resolve(target_path)
        exists = resolved.is_file()
        return EnvironmentStateCheck(
            check_name="file_exists",
            passed=exists,
            target_path=str(target_path),
            expected=True,
            actual=exists,
            message=f"File exists: {resolved}" if exists else f"File not found: {resolved}",
        )

    def verify_file_not_exists(self, target_path: Path | str) -> EnvironmentStateCheck:
        """指定ファイルが存在しない（削除された）かチェック"""
        resolved = self._resolve(target_path)
        not_exists = not resolved.exists()
        return EnvironmentStateCheck(
            check_name="file_not_exists",
            passed=not_exists,
            target_path=str(target_path),
            expected=True,
            actual=not_exists,
            message="File does not exist as expected" if not_exists else f"Unexpected file found: {resolved}",
        )

    def verify_file_content_contains(
        self,
        target_path: Path | str,
        expected_substring: str,
    ) -> EnvironmentStateCheck:
        """ファイル内に指定文字列が含まれているかチェック"""
        resolved = self._resolve(target_path)
        if not resolved.is_file():
            return EnvironmentStateCheck(
                check_name="file_content_contains",
                passed=False,
                target_path=str(target_path),
                expected=expected_substring,
                actual=None,
                message=f"File not found: {resolved}",
            )

        content = resolved.read_text(encoding="utf-8", errors="replace")
        passed = expected_substring in content
        return EnvironmentStateCheck(
            check_name="file_content_contains",
            passed=passed,
            target_path=str(target_path),
            expected=expected_substring,
            actual=content[:100],
            message=f"Substring '{expected_substring}' found in {resolved}" if passed else f"Substring '{expected_substring}' not found in {resolved}",
        )

    def capture_snapshot(self, target_dir: Path | str | None = None) -> dict[str, str]:
        """ディレクトリ内のファイル一覧とSHA256ハッシュのスナップショットを取得"""
        root = self._resolve(target_dir) if target_dir else (self.base_dir or Path.cwd())
        snapshot: dict[str, str] = {}
        if not root.is_dir():
            return snapshot

        for p in root.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(root))
                content = p.read_bytes()
                snapshot[rel] = hashlib.sha256(content).hexdigest()
        return snapshot

    def verify_snapshot_diff(
        self,
        before: dict[str, str],
        after: dict[str, str],
        expected_created: Sequence[str] = (),
        expected_modified: Sequence[str] = (),
    ) -> list[EnvironmentStateCheck]:
        """実行前後のスナップショットから期待されるファイル作成・変更を検証"""
        checks: list[EnvironmentStateCheck] = []

        # 作成されたファイルチェック
        for path in expected_created:
            passed = path not in before and path in after
            checks.append(EnvironmentStateCheck(
                check_name="expected_file_created",
                passed=passed,
                target_path=path,
                expected="created",
                actual="created" if passed else ("not_created" if path not in after else "already_existed"),
                message=f"File '{path}' correctly created" if passed else f"Failed to create '{path}'",
            ))

        # 変更されたファイルチェック
        for path in expected_modified:
            passed = path in before and path in after and before[path] != after[path]
            checks.append(EnvironmentStateCheck(
                check_name="expected_file_modified",
                passed=passed,
                target_path=path,
                expected="modified",
                actual="modified" if passed else "not_modified",
                message=f"File '{path}' correctly modified" if passed else f"Failed to modify '{path}'",
            ))

        return checks
