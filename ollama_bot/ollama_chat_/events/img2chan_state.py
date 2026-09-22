"""img.2chan.netスレ監視中の状態、投稿履歴、引用返信候補を管理するMixin。
自分の投稿検出、履歴JSONL保存、フォローアップ返信タスクの起動準備を担当します。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import discord

from ...common.discord_helpers import author_id, channel_id
from ...common.ollama_helpers import _normalize_compare_text
from .img2chan_context import (
    _get_img2chan_helper,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    json_dumps,
    log,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _Img2chanStateBase = OllamaChatProtocol
else:
    _Img2chanStateBase = object


class _Img2chanStateMixin(_Img2chanStateBase):
    def _prune_img2chan_post_schedules(self) -> None:
        tasks, recent, states = self._ensure_img2chan_thread_tracking_state()
        for url, task in list(tasks.items()):
            if task is None or task.done():
                tasks.pop(url, None)
        ttl_sec = max(cfg_float("IMG2CHAN_AUTO_POST_DEDUP_TTL_SEC", 3600.0), 60.0)
        now = time.monotonic()
        for url, scheduled_at in list(recent.items()):
            try:
                age = now - float(scheduled_at)
            except Exception:
                age = ttl_sec + 1
            if age > ttl_sec:
                recent.pop(url, None)
        for state in list(states.values()):
            if isinstance(state, dict):
                self._prune_img2chan_followup_reply_tasks(state)
        active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
        if active_url and active_url not in tasks:
            self._img2chan_active_thread_url = ""
            states.pop(active_url, None)

    def _ensure_img2chan_thread_tracking_state(
        self,
    ) -> tuple[dict[str, asyncio.Task], dict[str, float], dict[str, dict[str, object]]]:
        tasks = getattr(self, "_img2chan_post_tasks", None)
        if not isinstance(tasks, dict):
            self._img2chan_post_tasks = {}
            tasks = self._img2chan_post_tasks
        recent = getattr(self, "_img2chan_recent_schedule", None)
        if not isinstance(recent, dict):
            self._img2chan_recent_schedule = {}
            recent = self._img2chan_recent_schedule
        states = getattr(self, "_img2chan_thread_states", None)
        if not isinstance(states, dict):
            self._img2chan_thread_states = {}
            states = self._img2chan_thread_states
        active_url = getattr(self, "_img2chan_active_thread_url", "")
        if not isinstance(active_url, str):
            self._img2chan_active_thread_url = ""
        return tasks, recent, states

    def _img2chan_thread_state(self, url: str) -> dict[str, object]:
        _, _, states = self._ensure_img2chan_thread_tracking_state()
        state = states.get(url)
        if not isinstance(state, dict):
            state = {
                "url": url,
                "known_reply_post_nos": set(),
                "own_post_nos": set(),
                "own_post_texts": {},
                "pending_own_post_comments": [],
                "soudane_by_post_no": {},
                "analyzed_soudane_by_post_no": {},
                "own_post_feedback_milestones": {},
                "handled_quote_reply_post_nos": set(),
                "inflight_quote_reply_post_nos": set(),
                "reply_tasks": {},
            }
            states[url] = state
            return state
        state.setdefault("known_reply_post_nos", set())
        state.setdefault("own_post_nos", set())
        state.setdefault("own_post_texts", {})
        state.setdefault("pending_own_post_comments", [])
        state.setdefault("soudane_by_post_no", {})
        state.setdefault("analyzed_soudane_by_post_no", {})
        state.setdefault("own_post_feedback_milestones", {})
        state.setdefault("handled_quote_reply_post_nos", set())
        state.setdefault("inflight_quote_reply_post_nos", set())
        state.setdefault("reply_tasks", {})
        return state

    def _img2chan_followup_reply_limit(self) -> int:
        return max(cfg_int("IMG2CHAN_FOLLOWUP_REPLY_MAX_ACTIVE_TASKS", 2), 1)

    def _img2chan_active_thread_limit(self) -> int:
        configured = max(cfg_int("IMG2CHAN_AUTO_POST_MAX_ACTIVE_TASKS", 2), 1)
        if cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True):
            return 1
        return configured

    def _img2chan_thread_monitor_poll_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_THREAD_MONITOR_POLL_SEC", 60.0), 5.0)

    def _img2chan_thread_monitor_max_replies(self) -> int:
        return max(
            cfg_int("IMG2CHAN_THREAD_MONITOR_MAX_REPLIES", 80),
            cfg_int("IMG2CHAN_POST_CONTEXT_REPLIES", 10),
            10,
        )

    def _img2chan_post_context_replies(self) -> int:
        return max(cfg_int("IMG2CHAN_POST_CONTEXT_REPLIES", 10), 1)

    def _prune_img2chan_followup_reply_tasks(self, thread_state: dict[str, Any]) -> None:
        raw_tasks = thread_state.get("reply_tasks")
        reply_tasks: dict[Any, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
        thread_state["reply_tasks"] = reply_tasks
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight
        for post_no, task in list(reply_tasks.items()):
            if task is None or task.done():
                reply_tasks.pop(post_no, None)
                inflight.discard(post_no)

    def _img2chan_track_known_replies(self, thread_state: dict[str, Any], thread: dict[str, Any]) -> None:
        raw_known = thread_state.get("known_reply_post_nos")
        known_reply_post_nos: set[Any] = raw_known if isinstance(raw_known, set) else set()
        thread_state["known_reply_post_nos"] = known_reply_post_nos
        for reply in list(thread.get("replies") or []):
            post_no = str(reply.get("post_no") or "").strip()
            if post_no:
                known_reply_post_nos.add(post_no)

    def _register_img2chan_own_post(self, thread_state: dict[str, Any], reply: dict[str, Any]) -> None:
        post_no = str(reply.get("post_no") or "").strip()
        text = str(reply.get("text") or "").strip()
        if not post_no or not text:
            return
        raw_own_nos = thread_state.get("own_post_nos")
        own_post_nos: set[Any] = raw_own_nos if isinstance(raw_own_nos, set) else set()
        thread_state["own_post_nos"] = own_post_nos
        raw_texts = thread_state.get("own_post_texts")
        own_post_texts: dict[Any, Any] = raw_texts if isinstance(raw_texts, dict) else {}
        thread_state["own_post_texts"] = own_post_texts
        raw_known = thread_state.get("known_reply_post_nos")
        known_reply_post_nos: set[Any] = raw_known if isinstance(raw_known, set) else set()
        thread_state["known_reply_post_nos"] = known_reply_post_nos
        own_post_nos.add(post_no)
        own_post_texts[post_no] = text
        known_reply_post_nos.add(post_no)
        pending = thread_state.get("pending_own_post_comments")
        if isinstance(pending, list):
            normalized_text = _normalize_compare_text(text)
            thread_state["pending_own_post_comments"] = [
                item
                for item in pending
                if _normalize_compare_text(str((item or {}).get("comment") or "")) != normalized_text
            ]

    def _remember_img2chan_pending_own_post(self, thread_state: dict[str, Any], comment: str) -> None:
        normalized_comment = str(comment or "").strip()
        if not normalized_comment:
            return
        pending = thread_state.get("pending_own_post_comments")
        if not isinstance(pending, list):
            thread_state["pending_own_post_comments"] = []
            pending = thread_state["pending_own_post_comments"]

        normalized_key = _normalize_compare_text(normalized_comment)
        now = time.time()
        deduped: list[dict[str, Any]] = []
        for item in list(pending):
            if not isinstance(item, dict):
                continue
            existing_comment = str(item.get("comment") or "").strip()
            if not existing_comment:
                continue
            created_at = float(item.get("created_at", 0.0) or 0.0)
            if created_at > 0.0 and now - created_at > 3600.0:
                continue
            if _normalize_compare_text(existing_comment) == normalized_key:
                item["created_at"] = now
                deduped.append(item)
            else:
                deduped.append(item)
        if not any(_normalize_compare_text(str(item.get("comment") or "")) == normalized_key for item in deduped if isinstance(item, dict)):
            deduped.append({"comment": normalized_comment, "created_at": now})
        thread_state["pending_own_post_comments"] = deduped[-8:]

    def _match_img2chan_pending_own_posts(
        self,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> list[dict[str, Any]]:
        pending = thread_state.get("pending_own_post_comments")
        if not isinstance(pending, list) or not pending:
            return []
        helper = _get_img2chan_helper()
        finder = getattr(helper, "find_img_thread_reply_by_comment", None) if helper is not None else None
        if not callable(finder):
            return []

        matched_replies: list[dict[str, Any]] = []
        raw_known = thread_state.get("known_reply_post_nos")
        known_reply_post_nos: set[Any] = raw_known if isinstance(raw_known, set) else set()
        thread_state["known_reply_post_nos"] = known_reply_post_nos

        remaining: list[dict[str, Any]] = []
        for item in list(pending):
            if not isinstance(item, dict):
                continue
            comment = str(item.get("comment") or "").strip()
            if not comment:
                continue
            matched_reply = finder(
                thread,
                comment,
                exclude_post_numbers=set(known_reply_post_nos),
            )
            if matched_reply:
                self._register_img2chan_own_post(thread_state, matched_reply)
                matched_replies.append(dict(matched_reply))
            else:
                remaining.append(item)

        thread_state["pending_own_post_comments"] = remaining[-8:]
        return matched_replies

    def _build_img2chan_learning_context(
        self,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> dict[str, Any]:
        own_post_texts = thread_state.get("own_post_texts")
        if not isinstance(own_post_texts, dict):
            own_post_texts = {}
        own_post_nos = thread_state.get("own_post_nos")
        if not isinstance(own_post_nos, set):
            own_post_nos = set()
        helper = _get_img2chan_helper()
        quoted_post_resolver = getattr(helper, "find_img_thread_quoted_posts", None) if helper is not None else None
        pairs: list[dict[str, Any]] = []
        replies = list(thread.get("replies") or [])
        for reply in replies:
            post_no = str(reply.get("post_no") or "").strip()
            if not post_no or post_no in own_post_nos:
                continue
            reply_text = str(reply.get("text") or "").strip()
            if not reply_text:
                continue
            quoted_posts: list[dict[str, Any]] = []
            if callable(quoted_post_resolver):
                with contextlib.suppress(Exception):
                    quoted_posts = list(quoted_post_resolver(thread, reply, max_targets=2) or [])
            for qp in quoted_posts:
                qp_no = str(qp.get("post_no") or "").strip()
                if qp_no and qp_no in own_post_nos and qp_no in own_post_texts:
                    pairs.append({
                        "own_post_no": qp_no,
                        "own_text": self._clean_img2chan_learning_text(own_post_texts[qp_no]),
                        "other_post_no": post_no,
                        "other_text": self._clean_img2chan_learning_text(reply_text),
                    })
        return {
            "title": str(thread.get("title") or ""),
            "pairs": pairs[:6],
        }

    async def _store_img2chan_post_record(
        self,
        message: discord.Message,
        *,
        url: str,
        comment: str,
        target_reply: dict[str, Any] | None,
        registered_post_no: str = "",
        dry_run: bool = False,
    ) -> None:
        if dry_run:
            return

        await self._append_img2chan_post_history_entry(
            message,
            url=url,
            comment=comment,
            target_reply=target_reply,
            registered_post_no=registered_post_no,
        )

        add_episode = getattr(self.memory_store, "add_episode", None)
        if not callable(add_episode):
            return

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip() or "default"
        guild_id = getattr(message.guild, "id", None)
        discord_channel_id = channel_id(message)
        post_text = self._clean_img2chan_learning_text(comment, max_chars=340) or "(本文なし)"
        target_summary = ""
        if isinstance(target_reply, dict):
            target_post_no = str(target_reply.get("post_no") or "").strip()
            target_reply_no = int(target_reply.get("number") or 0)
            target_label = ""
            if target_reply_no > 0:
                target_label = f">>{target_reply_no}"
            if target_post_no:
                target_label = f"{target_label} No.{target_post_no}".strip()
            target_text = self._clean_img2chan_learning_text(target_reply.get("text"), max_chars=120)
            target_summary = f" / 返信先: {target_label or '(番号不明)'} {target_text}".strip()

        try:
            await add_episode(
                persona=persona,
                guild_id=guild_id,
                channel_id=discord_channel_id,
                user_id=None,
                summary="imgに次のレスを書いた。",
                what=f"URL: {url}{target_summary}"[:600],
                bot_action=post_text,
                user_reaction=f"registered_post_no={registered_post_no}"[:600] if registered_post_no else None,
                importance=0.62,
                tags=["img", "posted", "own_post"],
            )
        except Exception as e:
            log.exception("img2chan post record store failed url=%s err=%r", url, e)

    @staticmethod
    def _write_img2chan_post_history_line(path: str, line: str) -> None:
        target_path = Path(path).expanduser()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as f:
            f.write(line)

    async def _append_img2chan_post_history_entry(
        self,
        message: discord.Message,
        *,
        url: str,
        comment: str,
        target_reply: dict[str, Any] | None,
        registered_post_no: str = "",
    ) -> None:
        history_path = str(
            cfg("IMG2CHAN_POST_HISTORY_PATH", "/var/lib/ollama-bot/img2chan_post_history.jsonl") or ""
        ).strip()
        if not history_path:
            return

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip() or "default"
        target_payload: dict[str, Any] | None = None
        if isinstance(target_reply, dict):
            target_payload = {
                "post_no": str(target_reply.get("post_no") or "").strip() or None,
                "reply_number": int(target_reply.get("number") or 0) or None,
                "text": self._clean_img2chan_learning_text(target_reply.get("text"), max_chars=160) or None,
            }

        entry = {
            "event": "img2chan_post",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "persona": persona,
            "bot_name": str(getattr(getattr(self.bot, "user", None), "display_name", "") or ""),
            "discord": {
                "guild_id": getattr(getattr(message, "guild", None), "id", None),
                "channel_id": channel_id(message),
                "message_id": getattr(message, "id", None),
                "author_id": author_id(message),
            },
            "img_url": str(url or "").strip(),
            "comment": str(comment or "").strip(),
            "registered_post_no": str(registered_post_no or "").strip() or None,
            "target_reply": target_payload,
        }
        line = json_dumps(entry, ensure_ascii=False) + "\n"
        try:
            await asyncio.to_thread(self._write_img2chan_post_history_line, history_path, line)
        except Exception as e:
            log.exception("img2chan post history append failed path=%s err=%r", history_path, e)

    def _img2chan_thread_dropped(self, error: object) -> bool:
        normalized = str(error or "").strip().lower()
        if not normalized:
            return False
        return normalized.startswith("http 404") or normalized.startswith("http 410")

    def _build_img2chan_quoted_own_post_context(
        self,
        thread_state: dict[str, Any],
        quoted_own_post_numbers: object,
    ) -> list[dict[str, Any]]:
        own_post_texts = thread_state.get("own_post_texts")
        if not isinstance(own_post_texts, dict):
            return []
        items: list[dict[str, Any]] = []
        raw_quoted = quoted_own_post_numbers if isinstance(quoted_own_post_numbers, (list, tuple, set)) else []
        for post_no in list(raw_quoted)[:2]:
            normalized_post_no = str(post_no or "").strip()
            text = str(own_post_texts.get(normalized_post_no) or "").strip()
            if not normalized_post_no or not text:
                continue
            items.append({"post_no": normalized_post_no, "text": text})
        return items

    def _discover_img2chan_quote_reply_candidates(
        self,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> list[dict[str, Any]]:
        helper = _get_img2chan_helper()
        matcher = getattr(helper, "reply_quotes_img_post_text", None) if helper is not None else None
        quoted_post_resolver = getattr(helper, "find_img_thread_quoted_posts", None) if helper is not None else None
        if not callable(matcher):
            return []

        raw_known = thread_state.get("known_reply_post_nos")
        known_reply_post_nos: set[Any] = raw_known if isinstance(raw_known, set) else set()
        thread_state["known_reply_post_nos"] = known_reply_post_nos
        raw_own_nos = thread_state.get("own_post_nos")
        own_post_nos: set[Any] = raw_own_nos if isinstance(raw_own_nos, set) else set()
        thread_state["own_post_nos"] = own_post_nos
        raw_texts = thread_state.get("own_post_texts")
        own_post_texts: dict[Any, Any] = raw_texts if isinstance(raw_texts, dict) else {}
        thread_state["own_post_texts"] = own_post_texts
        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight

        candidates: list[dict[str, Any]] = []
        for reply in list(thread.get("replies") or []):
            post_no = str(reply.get("post_no") or "").strip()
            if not post_no:
                continue
            is_new_reply = post_no not in known_reply_post_nos
            known_reply_post_nos.add(post_no)
            if not is_new_reply:
                continue
            if post_no in own_post_nos or post_no in handled or post_no in inflight:
                continue
            reply_text = str(reply.get("text") or "").strip()
            if not reply_text:
                continue
            quoted_own_post_numbers: list[str] = []
            if callable(quoted_post_resolver):
                with contextlib.suppress(Exception):
                    quoted_posts = list(quoted_post_resolver(thread, reply, max_targets=2) or [])
                    for quoted_post in quoted_posts:
                        own_post_no = str(quoted_post.get("post_no") or "").strip()
                        if own_post_no and own_post_no in own_post_nos and own_post_no not in quoted_own_post_numbers:
                            quoted_own_post_numbers.append(own_post_no)
            for own_post_no, own_text in list(own_post_texts.items()):
                own_post_no_str = str(own_post_no or "").strip()
                if not own_post_no_str or not str(own_text or "").strip():
                    continue
                if own_post_no_str in quoted_own_post_numbers:
                    continue
                with contextlib.suppress(Exception):
                    if matcher(reply_text, own_text):
                        quoted_own_post_numbers.append(own_post_no_str)
            if quoted_own_post_numbers:
                candidates.append({
                    "reply": dict(reply),
                    "quoted_own_post_numbers": quoted_own_post_numbers,
                })
        return candidates

    def _schedule_img2chan_followup_reply_tasks(
        self,
        message: discord.Message,
        url: str,
        thread_state: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> None:
        self._prune_img2chan_followup_reply_tasks(thread_state)
        raw_tasks = thread_state.get("reply_tasks")
        reply_tasks: dict[Any, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
        thread_state["reply_tasks"] = reply_tasks
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight

        max_parallel = self._img2chan_followup_reply_limit()
        active_count = sum(1 for task in reply_tasks.values() if task is not None and not task.done())
        for candidate in list(candidates or []):
            if active_count >= max_parallel:
                break
            target_reply = dict(candidate.get("reply") or {})
            target_post_no = str(target_reply.get("post_no") or "").strip()
            if not target_post_no or target_post_no in inflight:
                continue
            inflight.add(target_post_no)
            quoted_own_posts = self._build_img2chan_quoted_own_post_context(
                thread_state,
                candidate.get("quoted_own_post_numbers"),
            )
            reply_tasks[target_post_no] = asyncio.create_task(
                self._run_img2chan_followup_reply_task(
                    message,
                    url,
                    thread_state=thread_state,
                    target_reply=target_reply,
                    quoted_own_posts=quoted_own_posts,
                )
            )
            active_count += 1
            log.info(
                "img2chan followup reply scheduled url=%s target_post_no=%s active=%d",
                url,
                target_post_no,
                active_count,
            )
