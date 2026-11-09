from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("lib.error_report")


class ErrorReporter:
    """例外の集計および通知を行うクラス。"""

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._notified_keys: set[str] = set()
        self._window_start = time_fn()
        self._time_fn = time_fn
        self._notifier: Callable[[str, int, BaseException], None] | None = None

    def set_notifier(self, fn: Callable[[str, int, BaseException], None] | None) -> None:
        """集計閾値到達時に呼び出す通知コールバックを設定する。"""
        with self._lock:
            self._notifier = fn

    def record(self, site: str, exc: BaseException) -> None:
        """発生した例外を記録・集計し、必要に応じてログ出力や通知を行う。
        いかなる内部例外も外部へ漏らさない完全防御設計。
        """
        try:
            try:
                from ollama_bot.common.config_helpers import cfg_bool, cfg_int
                enabled = cfg_bool("ERROR_REPORT_ENABLED", True)
                interval_sec = max(cfg_int("ERROR_REPORT_INTERVAL_SEC", 300), 10)
                threshold = max(cfg_int("ERROR_REPORT_NOTIFY_THRESHOLD", 20), 1)
            except Exception:
                # ollama_bot が存在しない単体実行・テスト環境向けフォールバック
                raw_enabled = os.environ.get("ERROR_REPORT_ENABLED", "true").strip().lower()
                enabled = raw_enabled not in {"0", "false", "no", "off"}
                try:
                    interval_sec = max(int(os.environ.get("ERROR_REPORT_INTERVAL_SEC", "300")), 10)
                except (ValueError, TypeError):
                    interval_sec = 300
                try:
                    threshold = max(int(os.environ.get("ERROR_REPORT_NOTIFY_THRESHOLD", "20")), 1)
                except (ValueError, TypeError):
                    threshold = 20

            if not enabled:
                return
        except Exception:
            interval_sec = 300
            threshold = 20

        try:
            now = self._time_fn()
            key = f"{site}|{type(exc).__name__}"
            notifier_to_call: Callable[[str, int, BaseException], None] | None = None
            current_count = 0

            with self._lock:
                if now - self._window_start >= interval_sec:
                    if self._counts:
                        log.warning("[ERROR_REPORT] Exception summary for last %ds: %s", interval_sec, self._counts)
                    self._counts.clear()
                    self._notified_keys.clear()
                    self._window_start = now

                self._counts[key] = self._counts.get(key, 0) + 1
                current_count = self._counts[key]

                if current_count >= threshold and key not in self._notified_keys:
                    self._notified_keys.add(key)
                    notifier_to_call = self._notifier

            if notifier_to_call is not None:
                try:
                    notifier_to_call(site, current_count, exc)
                except Exception as ne:
                    log.error("[ERROR_REPORT] notifier execution failed: %r", ne)
        except BaseException as outer_e:
            log.error("[ERROR_REPORT] record() internal failure: %r", outer_e)

    def summary(self) -> dict[str, int]:
        """現在集計中の例外カウント概要を取得する。"""
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        """集計カウント、通知済みキー、ウィンドウ開始時刻を初期化する。"""
        with self._lock:
            self._counts.clear()
            self._notified_keys.clear()
            self._window_start = self._time_fn()


_global_reporter: ErrorReporter | None = None
_global_reporter_lock = threading.Lock()


def get_error_reporter() -> ErrorReporter:
    """プロセス内シングルトンのErrorReporterインスタンスを取得する。"""
    global _global_reporter
    with _global_reporter_lock:
        if _global_reporter is None:
            _global_reporter = ErrorReporter()
        return _global_reporter


def report_error(site: str, exc: BaseException) -> None:
    """例外をErrorReporterに記録する便利関数。絶対に例外を外部へ漏らさない。"""
    try:
        get_error_reporter().record(site, exc)
    except BaseException:
        pass
