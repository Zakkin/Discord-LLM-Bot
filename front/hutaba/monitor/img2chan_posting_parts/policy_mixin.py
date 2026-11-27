"""設定参照・Discord返信譲歩・スレフィルタ・荒らし検知・宣伝付与・オウム返し判定 Mixin。"""
from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import random
import re
import time
from typing import Any

import discord

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _Img2chanPostingBase,
    _get_img2chan_helper,
    author_id,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    channel_id,
    log,
    looks_like_parrot_reply,
    truncate_text,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _HUTABA_CONSULTATION_RE,
    _extract_hutaba_non_quote_body,
)
from front.hutaba.monitor.img2chan_posting_parts.text_guards import (
    ParrotReason,
    _HUTABA_META_COMMENTARY_RE,
    _HUTABA_META_SEARCH_REPORT_RE,
    _SELF_PROJECTION_SYMPATHY_RE,
    _SOLUTION_HINT_MARKER_RE,
    _is_hutaba_thread_expiring_soon,
    _is_img2chan_request_thread,
    _is_img2chan_stream_or_live_thread,
    _normalize_for_sibling_plagiarism_check,
    _parse_hutaba_post_no_int,
    _sanitize_hutaba_reply_text,
    find_chimera_sentences,
)


class _Img2chanPolicyMixin(_Img2chanPostingBase):
    def _img2chan_proactive_post_enabled(self) -> bool:
        return cfg_bool("HUTABA_PROACTIVE_POST_ENABLED", True)

    def _img2chan_proactive_post_interval_min_sec(self) -> float:
        return max(cfg_float("HUTABA_PROACTIVE_POST_INTERVAL_MIN_SEC", 1800.0), 10.0)

    def _img2chan_proactive_post_interval_max_sec(self) -> float:
        min_sec = self._img2chan_proactive_post_interval_min_sec()
        return max(cfg_float("HUTABA_PROACTIVE_POST_INTERVAL_MAX_SEC", 3600.0), min_sec)

    def _img2chan_autonomous_followup_cooldown_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC", 300.0), 30.0)

    def _is_discord_user_reply_busy(self) -> bool:
        """Discord側でユーザー返信が推論中・前処理中・またはキュー待機中であるかを判定する。"""
        busy_fn = getattr(self, "_is_user_reply_busy", None)
        if callable(busy_fn):
            try:
                if bool(busy_fn()):
                    return True
            except Exception:
                pass
        bot = getattr(self, "bot", None)
        if bot is not None:
            busy_fn_bot = getattr(bot, "_is_user_reply_busy", None)
            if callable(busy_fn_bot):
                try:
                    if bool(busy_fn_bot()):
                        return True
                except Exception:
                    pass
        if getattr(self, "_active_user_reply_count", 0) > 0:
            return True
        if bot is not None and getattr(bot, "_active_user_reply_count", 0) > 0:
            return True
        if getattr(self, "_active_user_reply_turn_count", 0) > 0:
            return True
        if bot is not None and getattr(bot, "_active_user_reply_turn_count", 0) > 0:
            return True
        q = getattr(self, "_reply_queue", None)
        if q is None and bot is not None:
            q = getattr(bot, "_reply_queue", None)
        if q is not None and hasattr(q, "qsize"):
            try:
                if int(q.qsize()) > 0:
                    return True
            except Exception:
                pass
        return False

    async def _wait_for_discord_user_replies_idle(self, timeout_sec: float = 30.0) -> None:
        """Discordのユーザー返信および返信キューが完全にアイドルになるまで待機・譲歩する。"""
        parent_yielder = getattr(super(), "_yield_to_user_replies", None)
        if callable(parent_yielder):
            await parent_yielder(timeout_sec=timeout_sec)
        bot = getattr(self, "bot", None)
        bot_yielder = getattr(bot, "_yield_to_user_replies", None) if bot is not None else None
        if callable(bot_yielder):
            await bot_yielder(timeout_sec=timeout_sec)

        start_ts = time.time()
        max_wait = max(timeout_sec, float(cfg("LLM_YIELD_MAX_WAIT_SEC", 300.0) or 300.0))
        if self._is_discord_user_reply_busy():
            log.info("[hutaba] Discord返信中・キュー待機中を検知、処理を譲歩・待機開始")
            while self._is_discord_user_reply_busy():
                if time.time() - start_ts >= max_wait:
                    log.warning("[hutaba] Discord返信アイドル待機がタイムアウトしました (%.1fs)", max_wait)
                    break
                done_event = getattr(self, "_user_reply_done_event", None)
                if done_event is None and bot is not None:
                    done_event = getattr(bot, "_user_reply_done_event", None)
                if done_event is not None and hasattr(done_event, "wait"):
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(done_event.wait(), timeout=0.3)
                else:
                    await asyncio.sleep(0.3)
            log.info("[hutaba] Discord返信完了または待機終了を検知")

    async def _yield_to_user_replies(self, timeout_sec: float = 30.0) -> None:
        """Discordユーザー返信最優先（Preemption）ガード。
        ユーザー返信生成中・推論前処理中・返信キュー待機中は即座に待機・譲歩する。
        """
        await self._wait_for_discord_user_replies_idle(timeout_sec=timeout_sec)

    def _img2chan_exclude_stream_threads(self) -> bool:
        getter = getattr(super(), "_img2chan_exclude_stream_threads", None)
        if callable(getter):
            return bool(getter())
        return cfg_bool("HUTABA_EXCLUDE_STREAM_THREADS", True)

    def _is_img2chan_stream_or_live_thread(self, thread_item: dict[str, Any]) -> str | None:
        checker = getattr(super(), "_is_img2chan_stream_or_live_thread", None)
        if callable(checker):
            return checker(thread_item)
        return _is_img2chan_stream_or_live_thread(thread_item)

    def _img2chan_exclude_request_threads(self) -> bool:
        getter = getattr(super(), "_img2chan_exclude_request_threads", None)
        if callable(getter):
            return bool(getter())
        return cfg_bool("HUTABA_EXCLUDE_REQUEST_THREADS", True)

    def _is_img2chan_request_thread(self, thread_item: dict[str, Any]) -> str | None:
        checker = getattr(super(), "_is_img2chan_request_thread", None)
        if callable(checker):
            return checker(thread_item)
        return _is_img2chan_request_thread(thread_item)

    def _is_hutaba_thread_expiring_soon(
        self,
        thread_or_item: dict[str, Any] | None,
        *,
        min_remaining_sec: float | None = None,
    ) -> str | None:
        return _is_hutaba_thread_expiring_soon(
            thread_or_item,
            min_remaining_sec=min_remaining_sec,
        )

    def _is_img2chan_thread_vandalized(self, url: str) -> bool:
        checker = getattr(super(), "_is_img2chan_thread_vandalized", None)
        if callable(checker):
            return bool(checker(url))
        vandalized = getattr(self, "_img2chan_vandalized_urls", None)
        return bool(isinstance(vandalized, set) and url in vandalized)

    def _mark_img2chan_thread_vandalized(self, url: str, reason: str = "") -> None:
        marker = getattr(super(), "_mark_img2chan_thread_vandalized", None)
        if callable(marker):
            marker(url, reason)
            return
        vandalized = getattr(self, "_img2chan_vandalized_urls", None)
        if isinstance(vandalized, set):
            vandalized.add(url)
        recent = getattr(self, "_img2chan_recent_schedule", None)
        if isinstance(recent, dict):
            recent[url] = time.monotonic()

    def _save_img2chan_checkpoint(self, thread_state: dict[str, Any], **kwargs: Any) -> None:
        saver = getattr(super(), "_save_img2chan_checkpoint", None)
        if callable(saver):
            saver(thread_state, **kwargs)
            return
        thread_state["checkpoint"] = {"saved_at": time.time(), **kwargs}

    def _restore_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> dict[str, Any] | None:
        restorer = getattr(super(), "_restore_img2chan_checkpoint", None)
        if callable(restorer):
            res = restorer(thread_state)
            return res if isinstance(res, dict) else None
        checkpoint = thread_state.get("checkpoint")
        return checkpoint if isinstance(checkpoint, dict) else None

    def _clear_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> None:
        cleaner = getattr(super(), "_clear_img2chan_checkpoint", None)
        if callable(cleaner):
            cleaner(thread_state)
            return
        thread_state["checkpoint"] = None

    def _detect_img2chan_thread_vandalism(
        self,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
    ) -> str | None:
        """荒らし・不毛スレを検知し、撤退理由文字列を返す（健全な場合は None）。"""
        from front.hutaba.monitor.img2chan_learning import _IMG2CHAN_VANDALISM_BLOCK_PATTERNS

        op_text = str((thread or {}).get("op_text") or "").strip()
        if op_text and _IMG2CHAN_VANDALISM_BLOCK_PATTERNS.search(op_text):
            return "op_blocked_pattern"

        replies = list((thread or {}).get("replies") or [])
        if not replies:
            return None

        recent_replies = replies[-20:]
        block_hit_count = 0
        reply_texts: list[str] = []

        for r in recent_replies:
            t = str(r.get("text") or "").strip()
            if not t:
                continue
            reply_texts.append(t)
            if _IMG2CHAN_VANDALISM_BLOCK_PATTERNS.search(t):
                block_hit_count += 1

        if reply_texts and (block_hit_count / len(reply_texts)) >= 0.30:
            return f"high_toxicity_ratio ({block_hit_count}/{len(reply_texts)})"

        if len(reply_texts) >= 5:
            from collections import Counter
            counts = Counter(reply_texts)
            most_common_text, most_common_count = counts.most_common(1)[0]
            if most_common_count >= 4 or (len(reply_texts) >= 6 and most_common_count / len(reply_texts) >= 0.5):
                return f"script_spam_detected (repeated {most_common_count} times)"

        symbol_heavy_count = 0
        for t in reply_texts:
            if len(t) >= 10:
                alnum_jp = len(re.findall(r"[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", t))
                if alnum_jp / len(t) < 0.2:
                    symbol_heavy_count += 1
        if reply_texts and (symbol_heavy_count / len(reply_texts)) >= 0.40:
            return f"symbol_spam_bomb ({symbol_heavy_count}/{len(reply_texts)})"

        return None

    def _sanitize_hutaba_reply_text(self, text: str, max_chars: int = 120) -> str:
        """ふたば（img）向けレス文のサニタイズ。
        思考タグ除去、相手呼称（特定個人名）排除、Shift_JIS互換性維持、文字数制限。
        """
        return _sanitize_hutaba_reply_text(text, max_chars=max_chars)

    def _img2chan_character_guidance(self) -> str:
        parts: list[str] = []
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            parts.append(f"人格名: {display_name}")
        first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
        if first_person:
            parts.append(f"一人称: {first_person}")
        core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()
        if core_values:
            parts.append(f"核となる価値観・性格: {core_values}")
        for label, key in (
            ("口調ガード", "OLLAMA_REPLY_STYLE_GUARD"),
            ("img投稿追加ガード", "IMG2CHAN_POST_PERSONA_GUARD"),
        ):
            value = str(cfg(key, "") or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        if not parts:
            return ""
        guidance = "\n".join(parts)
        max_chars = max(cfg_int("IMG2CHAN_CHARACTER_GUIDANCE_MAX_CHARS", 1600), 200)
        return truncate_text(guidance, max_chars)

    def _img2chan_post_delay_sec(self) -> float:
        min_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MIN_DELAY_SEC", 60.0), 0.0)
        max_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MAX_DELAY_SEC", 1800.0), min_delay)
        return random.uniform(min_delay, max_delay)

    def _img2chan_post_generation_retries(self) -> int:
        return max(cfg_int("IMG2CHAN_POST_GENERATION_RETRIES", 1), 0)

    def _img2chan_post_generation_models(self) -> list[str]:
        models: list[str] = []
        for raw in (
            cfg("IMG2CHAN_POST_GENERATION_MODEL", cfg("OLLAMA_MODEL", "")),
            cfg("IMG2CHAN_POST_FALLBACK_MODEL", cfg("OLLAMA_MIDDLE_MODEL", "")),
        ):
            model = str(raw or "").strip()
            if model and model not in models:
                models.append(model)
        return models

    def _img2chan_promotion_enabled(self) -> bool:
        return cfg_bool("IMG2CHAN_PROMOTION_ENABLED", False)

    def _img2chan_promotion_probability(self) -> float:
        return max(0.0, min(cfg_float("IMG2CHAN_PROMOTION_PROBABILITY", 0.0), 1.0))

    def _img2chan_promotion_min_interval_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_PROMOTION_MIN_INTERVAL_SEC", 0.0), 0.0)

    def _img2chan_promotion_invite_url(self) -> str:
        invite_url = str(cfg("IMG2CHAN_PROMOTION_INVITE_URL", "") or "").strip()
        if invite_url and re.match(r"^discord\.gg/[A-Za-z0-9_-]+$", invite_url, flags=re.IGNORECASE):
            return f"https://{invite_url}"
        return invite_url

    def _img2chan_promotion_message(self) -> str:
        message = str(cfg("IMG2CHAN_PROMOTION_MESSAGE", "") or "").strip()
        if message:
            return message
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            return f"普段はDiscordの{display_name}でも喋ってる。"
        return "普段はDiscordでも喋ってる。"

    def _should_attach_img2chan_promotion(self) -> bool:
        if not self._img2chan_promotion_enabled():
            return False
        if not self._img2chan_promotion_invite_url():
            return False
        probability = self._img2chan_promotion_probability()
        if probability <= 0.0:
            return False
        last_sent_at = float(getattr(self, "_img2chan_last_promotion_at", 0.0) or 0.0)
        min_interval_sec = self._img2chan_promotion_min_interval_sec()
        if last_sent_at > 0.0 and min_interval_sec > 0.0 and time.time() - last_sent_at < min_interval_sec:
            return False
        return random.random() < probability

    def _log_img2chan_thread_analysis(
        self,
        *,
        context: str,
        url: str,
        thread: dict[str, Any],
        message: discord.Message | None = None,
    ) -> None:
        helper = _get_img2chan_helper()
        formatter = getattr(helper, "format_img_thread_log_summary", None) if helper is not None else None
        try:
            if callable(formatter):
                summary = formatter(
                    thread,
                    max_replies=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_REPLIES", 5), 0),
                    max_chars=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_CHARS", 1400), 300),
                )
            else:
                summary = (
                    f"IMG thread parse url={url} "
                    f"thread_no={str((thread or {}).get('thread_no') or '') or '-'} "
                    f"replies={int((thread or {}).get('total_reply_count') or 0)} "
                    f"fetched={int((thread or {}).get('fetched_reply_count') or 0)} "
                    f"op={truncate_text(str((thread or {}).get('op_text') or '').strip(), 160)!r}"
                )
        except Exception as e:
            log.warning("[hutaba] スレッド分析ログ整形失敗 url=%s err=%r", url, e)
            return

        log.info(
            "[hutaba] スレッド分析完了 context=%s channel=%s author=%s\n%s",
            context,
            channel_id(message) if message is not None else None,
            author_id(message) if message is not None else None,
            summary,
        )

    def _mark_img2chan_promotion_sent(self) -> None:
        self._img2chan_last_promotion_at = time.time()

    def _maybe_append_img2chan_promotion(
        self,
        helper: Any,
        comment: str,
    ) -> tuple[str, bool, str]:
        current_comment = str(comment or "").strip()
        invite_url = self._img2chan_promotion_invite_url()
        if not current_comment or not invite_url or not self._should_attach_img2chan_promotion():
            return current_comment, False, ""

        promo_message = self._img2chan_promotion_message()
        builder = getattr(helper, "append_img_post_promotion", None) if helper is not None else None
        if callable(builder):
            promoted_comment = builder(
                current_comment,
                promo_message,
                invite_url,
                max_chars=340,
            )
        else:
            promoted_comment = "\n".join(
                part for part in (current_comment, promo_message, invite_url) if str(part or "").strip()
            )
        promoted_comment = str(promoted_comment or "").strip()
        if not promoted_comment or invite_url not in promoted_comment:
            return current_comment, False, ""
        return promoted_comment, True, invite_url

    def _build_img2chan_followup_retry_prompt(
        self,
        base_prompt: str,
        *,
        target_reply: dict[str, Any] | None = None,
    ) -> str:
        excerpt = truncate_text(
            re.sub(r"\s+", " ", str((target_reply or {}).get("text") or "").strip()),
            120,
        )
        lines = [
            str(base_prompt or "").strip(),
            "",
            "追加ルール:",
            "- 直前の候補はオウム返し寄り、または不適切な検索報告・自己投影だったため破棄してください。",
            "- 今回返す対象レスの本文や語尾をそのままなぞるのは禁止です。",
            "- 対象レスの言い換えだけで終わらせず、自分の反応やツッコミを自分の言葉で入れてください。",
            "- 出力は引き続きJSONだけにし、comment には新しく書く本文だけを入れてください。",
            "- 対象レスの一部の行動や単語（例：『手順書を見直す』等）だけを拾って『私も毎回〜派なんですよ』『自分も〜するタイプです』のように主語を自分にすり替えて同調するだけの自己投影ミラーリングも禁止です。",
            "- 『調べてみたんですけど定義が確認できなかった』『〜のニュースばかり出てきた』『特定の作品のネタ？』といった検索失敗レポートや辞書解説は【絶対厳禁】です。事前調査メモにスレと無関係なノイズがある場合は完全に無視し、前後のレスの流れに対する等身大のツッコミ・共感レスを書いてください。",
            "- 自分の習慣語りにすり替えず、対象レスが『なぜその行動をとっているのか（背景・心理・AIとの主従逆転・周囲のツッコミ）』を踏まえて、相手の状況そのものへのツッコミや共感を書いてください。",
            "- 周囲のレス（他人のツッコミや判定結果）を『〇〇ってツッコミが来てますけど』のように他人の発言としてメタ実況・報告することは厳禁です。スレッドの空気として内在化し、自分自身のスタンスで語ってください。",
            "- 周囲のレスの単語を適当に繋ぎ合わせたキメラ疑問文（『〜なんですかね？』）は禁止です。相談にはズバッと結論ファーストでアドバイスしてください。",
            "- 【既出ツッコミ・回答の言い換えパクり・キメラ合体厳禁】すでに他のとしあきが書いているツッコミや回答（例: 『もう2割くらい下がってる』『がっつり下がって欲しいが無理だろな』等）を語尾だけ変えて繰り返したり、複数の他人のレスを繋ぎ合わせて自分の発言にすることは【絶対厳禁】です。他人が既に言った内容は避け、まだ出ていない別の視点・理由・リアクションを1つだけ自分の言葉で書いてください。ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。",
        ]
        if excerpt:
            lines.append(f"- 対象レスの本文をそのまま写したら失敗です。対象レス: {excerpt}")
        return "\n".join(lines).strip()

    def _img2chan_comment_parrot_reason(
        self,
        arg1: Any = None,
        arg2: Any = None,
        *,
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        thread: dict[str, Any] | None = None,
    ) -> ParrotReason | str | None:
        if isinstance(arg1, str):
            comment_text = arg1.strip()
            helper = None
            if isinstance(arg2, dict) and thread is None:
                thread = arg2
        else:
            helper = arg1
            comment_text = str(arg2 or "").strip()

        if not comment_text:
            return None

        checker = getattr(helper, "looks_like_img_reply_parrot", None) if helper is not None else None

        def _looks_like_reference_echo(reference_text: object) -> bool:
            reference = str(reference_text or "").strip()
            if not reference:
                return False
            if callable(checker):
                with contextlib.suppress(Exception):
                    if checker(comment_text, reference) is True:
                        return True
            return looks_like_parrot_reply(reference, comment_text)

        # 文頭フレーズ反復（Hook Echo）の検査
        raw_comment = comment_text.strip()
        first_token = re.split(r"[…!！?？、,。\s]", raw_comment)[0].strip()
        if isinstance(target_reply, dict) and len(first_token) >= 3:
            target_txt = str(target_reply.get("text") or "")
            if first_token in target_txt and re.match(rf"^{re.escape(first_token)}[…!！]+", raw_comment):
                if re.search(r"(?:熱さ|懐かし|恥ずかし|青春|時代|あの頃|確かに|分かります|そうですよね|もんね|ですね)", raw_comment):
                    return "hook_echo_abstract_poem"

        if isinstance(target_reply, dict) and _looks_like_reference_echo(target_reply.get("text")):
            return "target_reply"

        for own_post in list(quoted_own_posts or [])[:2]:
            if isinstance(own_post, dict) and _looks_like_reference_echo(own_post.get("text")):
                return "quoted_own_post"

        body = comment_text
        if _SELF_PROJECTION_SYMPATHY_RE.search(body):
            # 対象レスまたは引用元が相談・困りごとであり、かつ具体的な解決ヒント語が含まれていない場合
            ref_texts: list[str] = []
            if isinstance(target_reply, dict):
                ref_texts.append(str(target_reply.get("text") or ""))
            if isinstance(thread, dict):
                for r in (thread.get("replies") or [])[-25:]:
                    if isinstance(r, dict):
                        ref_texts.append(str(r.get("text") or ""))
            if any(_HUTABA_CONSULTATION_RE.search(t) for t in ref_texts):
                # 1文目（共感）を除いた後半に解決策の提案がない場合は自己投影ミラーリングと判定
                lines_no_quote = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith((">", "＞"))]
                non_sympathy_text = " ".join(ln for ln in lines_no_quote if not _SELF_PROJECTION_SYMPATHY_RE.search(ln))
                if not _SOLUTION_HINT_MARKER_RE.search(non_sympathy_text):
                    return "self_projection_sympathy_mirror"

        if _HUTABA_META_SEARCH_REPORT_RE.search(body):
            return "meta_search_failure_report"

        if _HUTABA_META_COMMENTARY_RE.search(body):
            return "meta_commentary_report"

        # 7. スレッド内の他人の既存レス（子レス・周辺レス）の語尾変えパクり・キメラ合体検知
        if isinstance(thread, dict):
            raw_pool = thread.get("all_replies") or thread.get("replies") or []
            if isinstance(raw_pool, list) and raw_pool:
                target_post_no = int((target_reply or {}).get("post_no") or (target_reply or {}).get("number") or 0)
                target_norm_core = (
                    _normalize_for_sibling_plagiarism_check(
                        str((target_reply or {}).get("body") or (target_reply or {}).get("text") or "")
                    )
                    if isinstance(target_reply, dict)
                    else ""
                )
                op_text = str(thread.get("op_text") or thread.get("title") or "")
                op_norm_core = _normalize_for_sibling_plagiarism_check(op_text)
                topic_shared_bigrams: set[str] = set()
                if len(target_norm_core) >= 2:
                    topic_shared_bigrams.update(
                        target_norm_core[i : i + 2] for i in range(len(target_norm_core) - 1)
                    )
                if len(op_norm_core) >= 2:
                    topic_shared_bigrams.update(
                        op_norm_core[i : i + 2] for i in range(len(op_norm_core) - 1)
                    )

                cleaned_comment = _extract_hutaba_non_quote_body(comment_text) or comment_text.strip()
                cand_core = _normalize_for_sibling_plagiarism_check(cleaned_comment)
                c_bigrams = {cand_core[i : i + 2] for i in range(len(cand_core) - 1)} if len(cand_core) >= 2 else set()
                c_unique_bigrams = c_bigrams - topic_shared_bigrams
                matched_sibling_post_nos: list[int] = []
                max_lcs_overall = 0

                seen_pnos: set[int] = set()
                dedup_pool: list[dict[str, Any]] = []
                for rep in raw_pool:
                    if not isinstance(rep, dict) or rep.get("is_op"):
                        continue
                    pno = int(rep.get("post_no") or rep.get("number") or 0)
                    if pno and pno in seen_pnos:
                        continue
                    if pno:
                        seen_pnos.add(pno)
                    dedup_pool.append(rep)

                for rep in dedup_pool:
                    if not isinstance(rep, dict):
                        continue
                    rep_no = int(rep.get("post_no") or rep.get("number") or 0)
                    if target_post_no and rep_no == target_post_no:
                        continue
                    rep_body = str(rep.get("body") or rep.get("text") or "").strip()
                    if not rep_body:
                        continue
                    rep_core = _normalize_for_sibling_plagiarism_check(rep_body)
                    if len(rep_core) < 6:
                        continue

                    # (A) 単一レスからの丸パクリ（6文字以上の既存レスがほぼそのまま含まれる、または8文字以上の共通部分文字列）
                    if len(rep_core) >= 8 and (rep_core in cand_core or cand_core in rep_core):
                        if not (target_norm_core and rep_core in target_norm_core):
                            lcs_val = min(len(cand_core), len(rep_core))
                            return ParrotReason(
                                "sibling_reply_plagiarism",
                                matched_sibling_post_nos=[rep_no],
                                longest_common_subsequence_len=lcs_val,
                            )

                    longest_common = 0
                    for length in range(min(len(cand_core), len(rep_core)), 6, -1):
                        found = False
                        for start in range(0, len(rep_core) - length + 1):
                            sub = rep_core[start : start + length]
                            if sub in cand_core:
                                # 対象レス自体やOPに含まれる共通話題語（固有名詞等）だけの一致なら除外
                                if (target_norm_core and sub in target_norm_core) or (
                                    op_norm_core and sub in op_norm_core
                                ):
                                    continue
                                longest_common = length
                                found = True
                                break
                        if found:
                            break

                    if longest_common > max_lcs_overall:
                        max_lcs_overall = longest_common

                    if longest_common >= 9:
                        return ParrotReason(
                            "sibling_reply_plagiarism",
                            matched_sibling_post_nos=[rep_no],
                            longest_common_subsequence_len=longest_common,
                        )

                    # (B) 複数レスからのつぎはぎ（キメラ合成）検知用の部分一致カウント
                    # 対象レス・OPの共通話題語を除いた固有2-gramで比較し、極短レス（8文字未満）での偶然一致を防ぐ
                    overlap_ratio = 0.0
                    if len(rep_core) >= 8 and len(c_unique_bigrams) >= 3:
                        r_bigrams = {rep_core[i : i + 2] for i in range(len(rep_core) - 1)}
                        r_unique_bigrams = r_bigrams - topic_shared_bigrams
                        if len(r_unique_bigrams) >= 4:
                            common_unique = len(c_unique_bigrams & r_unique_bigrams)
                            overlap_ratio = common_unique / max(1, min(len(c_unique_bigrams), len(r_unique_bigrams)))

                    if longest_common >= 7 or (overlap_ratio >= 0.60 and longest_common >= 5):
                        matched_sibling_post_nos.append(rep_no)

                if len(matched_sibling_post_nos) >= 2:
                    return ParrotReason(
                        "sibling_reply_plagiarism",
                        matched_sibling_post_nos=matched_sibling_post_nos,
                        longest_common_subsequence_len=max_lcs_overall,
                    )

                # 8. 固有語の出所によるキメラ検出（語を置換した言い換えは文字列一致では捕捉できない）
                target_text_for_chimera = (
                    str((target_reply or {}).get("body") or (target_reply or {}).get("text") or "")
                    if isinstance(target_reply, dict)
                    else ""
                )
                chimera_hits = find_chimera_sentences(
                    comment_text,
                    target_text=target_text_for_chimera,
                    op_text=op_text,
                    sibling_replies=[
                        rep
                        for rep in dedup_pool
                        if not (target_post_no and _parse_hutaba_post_no_int(rep) == target_post_no)
                    ],
                )
                if chimera_hits:
                    return ParrotReason(
                        "sibling_entity_chimera",
                        matched_sibling_post_nos=[chimera_hits[0]["matched_post_no"]],
                        longest_common_subsequence_len=0,
                    )

        return None
