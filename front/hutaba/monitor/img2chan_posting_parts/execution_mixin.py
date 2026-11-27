"""投稿後感情LLM生成・スレッド書き込み実行・Discord結果報告 Mixin。"""
from __future__ import annotations

import contextlib
import re
import time
from typing import Any

import discord

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _HOOKS_AVAILABLE,
    _IMG_THREAD_URL_PATTERN,
    _Img2chanPostingBase,
    _cfg_persona_namespace,
    _compact_exception_message,
    _get_img2chan_helper,
    _normalize_img_post_no,
    cfg,
    cfg_bool,
    cfg_float,
    discussed_topics,
    log,
    looks_like_abnormal_assistant_reply,
    truncate_text,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _has_hutaba_unsupported_media,
)
from front.hutaba.monitor.img2chan_posting_parts.text_guards import (
    _build_hutaba_post_emotion_reason,
)

if _HOOKS_AVAILABLE:
    from front.hutaba.monitor.img2chan_posting_parts.deps import (
        HookContext,
        POST_PRE_SEND,
        run_hooks,
    )


class _Img2chanExecutionMixin(_Img2chanPostingBase):
    async def _generate_hutaba_post_emotion_via_llm(
        self,
        *,
        url: str,
        thread: dict[str, Any] | None,
        comment: str,
        target_reply: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """スレッド書き込み直後の感情スコアと理由（reason）をLLM自身の言葉で動的生成する。"""
        json_caller = getattr(self, "_call_img2chan_ollama_json", None)
        if not callable(json_caller):
            log.info("[hutaba] 書き込み後感情のLLM生成をスキップ: json caller unavailable")
            return None

        scorer_fn = getattr(self, "_emotion_scorer_model_name", None)
        model_name = ""
        if callable(scorer_fn):
            with contextlib.suppress(Exception):
                model_name = str(scorer_fn() or "").strip()
        if not model_name:
            model_name = (
                str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
                or str(cfg("OLLAMA_CLASSIFIER_MODEL", "") or "").strip()
                or str(cfg("IMG_OLLAMA_MODEL", "") or "").strip()
                or str(cfg("OLLAMA_MODEL", "") or "").strip()
            )
        if not model_name:
            log.info("[hutaba] 書き込み後感情のLLM生成をスキップ: model unresolved")
            return None

        helper = _get_img2chan_helper()
        schema = getattr(helper, "HUTABA_POST_EMOTION_SCHEMA", None)
        build_sys = getattr(helper, "build_hutaba_post_emotion_system_prompt", None)
        build_usr = getattr(helper, "build_hutaba_post_emotion_prompt", None)

        guidance_fn = getattr(self, "_img2chan_character_guidance", None)
        persona_guidance = str(guidance_fn() or "") if callable(guidance_fn) else ""
        current_emotion_text = ""
        emotion_state = getattr(self, "emotion_state", None)
        if emotion_state is not None:
            cur_dom = str(getattr(emotion_state, "dominant_emotion", "neutral") or "neutral")
            cur_reason = str(getattr(emotion_state, "reason", "") or "")
            current_emotion_text = f"dominant={cur_dom}, reason={cur_reason}"

        board_srv = "may" if "may.2chan.net" in str(url or "").lower() else "img"

        if callable(build_sys):
            sys_prompt = str(build_sys(persona_guidance))
        else:
            sys_prompt = (
                "あなたは掲示板（ふたば☆ちゃんねる）にレスを書き込んだ直後のAI自身の内的感情と、"
                "その感情になった具体的な理由（reason）を分析・作文するモジュールです。"
                "定型文は使わず、今回のスレッドの話題・としあきの発言・自分が書いたレスを踏まえた具体的な理由を1文（句点『。』終わり）でJSON出力してください。"
            )

        if callable(build_usr):
            usr_prompt = str(
                build_usr(
                    thread,
                    comment,
                    target_reply=target_reply,
                    url=url,
                    board=board_srv,
                    character_guidance=persona_guidance,
                    current_emotion_text=current_emotion_text,
                )
            )
        else:
            usr_prompt = f"URL: {url}\n書き込み内容: {comment}"

        if not isinstance(schema, dict):
            schema = {
                "type": "object",
                "properties": {
                    "dominant_emotion": {"type": "string"},
                    "scores": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["dominant_emotion", "scores", "reason"],
            }

        try:
            payload = await json_caller(
                usr_prompt,
                purpose="hutaba_post_emotion",
                model=model_name,
                system_prompt=sys_prompt,
                schema=schema,
                temperature=0.35,
                num_predict=256,
                timeout_sec=30.0,
            )
        except Exception as e:
            log.warning("[hutaba] 書き込み後感情のLLM生成に失敗: %r", e)
            return None

        if not isinstance(payload, dict) or not payload:
            log.info("[hutaba] 書き込み後感情のLLM結果を不採用: reason=%s", "empty_payload")
            return None

        raw_reason = re.sub(r"\s+", " ", str(payload.get("reason") or "").strip()).strip()
        # 定型プレースホルダや短すぎる理由を弾く
        if len(raw_reason) < 10:
            log.info("[hutaba] 書き込み後感情のLLM結果を不採用: reason=%s", "reason_too_short")
            return None
        if any(
            bad in raw_reason
            for bad in ("理由を簡潔に", "具体的な理由", "JSON", "スキーマ")
        ):
            log.info("[hutaba] 書き込み後感情のLLM結果を不採用: reason=%s", "reason_placeholder")
            return None
        if not raw_reason.endswith(("。", "！", "!", "？", "?")):
            raw_reason += "。"
        elif not raw_reason.endswith("。"):
            raw_reason = raw_reason.rstrip("！!？?") + "。"

        valid_emotions = {
            "joy",
            "anticipation",
            "surprise",
            "trust",
            "sadness",
            "anger",
            "fear",
            "disgust",
            "neutral",
        }
        dom = str(payload.get("dominant_emotion") or "joy").strip().lower()
        if dom not in valid_emotions:
            dom = "joy"

        raw_scores = payload.get("scores")
        clean_scores: dict[str, float] = {}
        if isinstance(raw_scores, dict):
            for k in ("joy", "anticipation", "surprise", "trust", "sadness", "anger", "fear", "disgust"):
                val = raw_scores.get(k)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    clean_scores[k] = max(0.0, min(1.0, round(float(val), 4)))

        return {
            "dominant_emotion": dom,
            "scores": clean_scores,
            "reason": raw_reason,
        }

    async def _execute_img2chan_thread_post(
        self,
        message: discord.Message | None,
        url: str,
        *,
        delay_sec: float | None,
        thread_state: dict[str, Any] | None = None,
        requester_text: str = "",
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        report_result: bool = True,
    ) -> dict[str, Any]:
        helper = _get_img2chan_helper()
        if helper is None:
            return {
                "success": False,
                "url": url,
                "comment": "",
                "error": "img2chan helper が利用できません。",
            }

        await self._yield_to_user_replies()

        max_replies = max(self._img2chan_post_context_replies(), self._img2chan_thread_monitor_max_replies())
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        cached_caption = None
        if thread_state is not None and isinstance(thread_state, dict):
            cached_caption = str(thread_state.get("op_image_caption") or "").strip() or None
        try:
            try:
                thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=max_replies,
                    timeout_sec=timeout_sec,
                    cached_caption=cached_caption,
                )
            except TypeError:
                thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=max_replies,
                    timeout_sec=timeout_sec,
                )
        except Exception as e:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {e}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        error = str((thread or {}).get("error") or "").strip()
        if error:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {error}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if thread and isinstance(thread, dict) and thread_state is not None and isinstance(thread_state, dict):
            caption = str(thread.get("op_image_caption") or "").strip()
            if caption and not thread_state.get("op_image_caption"):
                thread_state["op_image_caption"] = caption

        self._log_img2chan_thread_analysis(
            context="followup_fetch" if target_reply is not None else "initial_fetch",
            url=url,
            thread=thread,
            message=message,
        )

        if thread_state is not None:
            self._img2chan_track_known_replies(thread_state, thread)

        own_nos_existing = thread_state.get("own_post_nos") or set() if thread_state else set()
        if thread_state and not bool(thread_state.get("user_requested")) and target_reply is None and not own_nos_existing:
            request_reason = self._is_img2chan_request_thread(thread)
            if request_reason:
                log.info(
                    "[hutaba] リクエスト系スレッドのため自発書き込みをスキップします: %s (reason=%s)",
                    url,
                    request_reason,
                )
                thread_state["status"] = "aborted"
                return {
                    "success": False,
                    "url": url,
                    "error": f"request_thread_detected:{request_reason}",
                }
            expire_reason = self._is_hutaba_thread_expiring_soon(thread)
            if expire_reason:
                log.info(
                    "[hutaba] スレッドが消滅間近（10分以内等）のため自発書き込みをスキップします: %s (reason=%s expire_text=%s)",
                    url,
                    expire_reason,
                    str((thread or {}).get("expire_text") or ""),
                )
                thread_state["status"] = "completed"
                return {
                    "success": False,
                    "url": url,
                    "error": f"thread_expiring_soon:{expire_reason}",
                }

        await self._yield_to_user_replies()

        # 進行中スレッド（10レス以上）での事前返信先確定ガード
        effective_target_reply = target_reply
        replies_list = list(thread.get("replies") or [])
        total_replies = int(thread.get("total_reply_count") or len(replies_list))
        is_active_thread = total_replies >= 10 or len(replies_list) >= 10

        if effective_target_reply is None and is_active_thread:
            counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
            reply_counts = counter_fn(thread) if callable(counter_fn) else {}
            best_hot_post: dict[str, Any] | None = None
            best_cnt = 0
            recent_window = [r for r in replies_list[-20:] if isinstance(r, dict)]
            # 直近20件以内のレスのみを事前ターゲット候補とする（古い1件被引用レスへの誤固定を防止）
            for cand in reversed(recent_window):
                if _has_hutaba_unsupported_media(str(cand.get("text") or cand.get("body") or "")):
                    continue
                c_no = _normalize_img_post_no(cand.get("post_no"))
                p_no_int = int(cand.get("post_no") or cand.get("number") or 0)
                cnt = int(reply_counts.get(c_no, 0) or reply_counts.get(p_no_int, 0) or 0)
                if cnt > best_cnt:
                    body_preview = str(cand.get("text") or cand.get("body") or "").strip()
                    if len(body_preview) >= 4:
                        best_cnt = cnt
                        best_hot_post = cand

            # 直近20件に被返信レスがない場合、全体からのフォールバックは cnt >= 2 の盛り上がりレスに限定
            if best_hot_post is None and reply_counts:
                for cand in reversed(replies_list):
                    if not isinstance(cand, dict) or _has_hutaba_unsupported_media(str(cand.get("text") or cand.get("body") or "")):
                        continue
                    c_no = _normalize_img_post_no(cand.get("post_no"))
                    p_no_int = int(cand.get("post_no") or cand.get("number") or 0)
                    cnt = int(reply_counts.get(c_no, 0) or reply_counts.get(p_no_int, 0) or 0)
                    if cnt >= 2 and cnt > best_cnt:
                        body_preview = str(cand.get("text") or cand.get("body") or "").strip()
                        if len(body_preview) >= 4:
                            best_cnt = cnt
                            best_hot_post = cand

            if best_hot_post is not None:
                effective_target_reply = best_hot_post
                log.info(
                    "[hutaba] 進行中スレのため最多被返信レスを事前ターゲットに確定: post_no=%s count=%d url=%s",
                    best_hot_post.get("post_no"),
                    best_cnt,
                    url,
                )

        try:
            comment, quote_post_numbers, quote_reply_numbers = await self._generate_hutaba_reply(
                message,
                url,
                thread=thread,
                thread_state=thread_state if thread_state is not None else {},
                target_reply=effective_target_reply,
                quoted_own_posts=quoted_own_posts,
                requester_text=requester_text,
            )
        except Exception as e:
            reason = _compact_exception_message(e)
            log.warning(
                "[hutaba] スレッド返信の生成に失敗したため投稿を中止しました: url=%s error=%s",
                url,
                reason,
            )
            result = {"success": False, "posted": False, "url": url, "comment": "", "error": f"投稿文生成に失敗しました: {reason}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "posted": False, "url": url, "comment": comment, "error": "投稿本文を作れませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if hasattr(helper, "compose_img_thread_reply_comment"):
            post_plan = getattr(helper, "compose_img_thread_reply_comment")(
                thread,
                comment,
                quote_post_numbers=quote_post_numbers,
                quote_reply_numbers=quote_reply_numbers,
                max_chars=340,
            )
            comment = str(post_plan.get("comment") or comment).strip()

        promotion_applied = False
        promotion_invite_url = ""
        comment, promotion_applied, promotion_invite_url = self._maybe_append_img2chan_promotion(
            helper,
            comment,
        )

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "posted": False, "url": url, "comment": comment, "error": "投稿本文を整形できませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        await self._yield_to_user_replies()

        if message is None and target_reply is None and not (thread_state or {}).get("user_requested") and not own_nos_existing and getattr(self, "memory_store", None) is not None and cfg_bool("DISCUSSED_TOPICS_ENABLED", True) and globals().get("discussed_topics") is not None:
            _dt_match: Any = None
            try:
                _dt_match = await discussed_topics.find_discussed_topic_match(getattr(self, "memory_store", None), persona=_cfg_persona_namespace(custom_cfg=cfg), candidate_text=comment)
            except Exception as _dt_exc:
                log.debug("[hutaba] 既出話題照合失敗(fail-open): %r", _dt_exc)
            if isinstance(_dt_match, dict) and _dt_match:
                log.info("[hutaba] 既出話題のため自発投稿を見送り: url=%s candidate=%r matched_summary=%r matched_at=%s", url, comment, _dt_match.get("summary", ""), _dt_match.get("created_at") or _dt_match.get("timestamp", ""))
                return {"success": False, "posted": False, "skipped": True, "url": url, "comment": comment, "error": ""}

        if _HOOKS_AVAILABLE:
            _hook_outcome = run_hooks(
                POST_PRE_SEND,
                HookContext(
                    point=POST_PRE_SEND,
                    front="hutaba",
                    text=comment,
                    metadata={
                        "thread_url": url,
                        "task_kind": "followup" if target_reply is not None else "initial",
                    },
                ),
            )
            if _hook_outcome.blocked:
                log.warning("[hutaba] フックにより投稿を見送り: reason=%s", _hook_outcome.reason)
                result = {
                    "success": False,
                    "posted": False,
                    "url": url,
                    "comment": comment,
                    "error": f"フックにより投稿を見送りました: {_hook_outcome.reason}",
                }
                if report_result:
                    await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
                return result
            comment = _hook_outcome.text

        is_dry_run = bool(cfg_bool("IMG2CHAN_POST_DRY_RUN", False))
        log.info(
            "[hutaba] スレッド書き込み開始: url=%s dry_run=%s comment=%r",
            url,
            is_dry_run,
            comment,
        )

        if is_dry_run:
            result = {
                "success": True,
                "posted": True,
                "dry_run": True,
                "url": url,
                "comment": comment,
                "error": "",
            }
        else:
            result = await getattr(helper, "submit_img_thread_reply")(
                url,
                comment,
                email=str(cfg("IMG2CHAN_POST_EMAIL", "") or ""),
                password=str(cfg("IMG2CHAN_POST_PASSWORD", "") or ""),
                timeout_sec=timeout_sec,
                preserve_urls=[promotion_invite_url] if promotion_applied and promotion_invite_url else None,
            )
            result["posted"] = bool(result.get("success"))

        if promotion_applied and (
            bool(result.get("dry_run"))
            or bool(result.get("success"))
            or bool(result.get("submitted"))
        ):
            self._mark_img2chan_promotion_sent()

        if bool(result.get("success")):
            log.info(
                "[hutaba] スレッド書き込み成功: url=%s dry_run=%s comment=%r",
                url,
                bool(result.get("dry_run")),
                comment,
            )
            if thread_state is not None:
                thread_state["last_own_post_at"] = time.time()

            # === 全フロント共通行動ログへの記録 ===
            memory_store = getattr(self, "memory_store", None)
            if memory_store is not None and bool(cfg("ACTIVITY_LOG_ENABLED", True)):
                add_act_fn = getattr(memory_store, "add_bot_activity", None)
                if callable(add_act_fn):
                    try:
                        from ollama_bot.common.config_helpers import cfg_persona_namespace
                        # スレッドURLから板名とスレ番号を抽出して source_ref を生成
                        source_ref = "img"
                        m_ref = _IMG_THREAD_URL_PATTERN.search(url)
                        if m_ref:
                            board = str(m_ref.group("server") or "img").lower()
                            t_no = str(m_ref.group("thread_no") or "")
                            source_ref = f"{board}/{t_no}" if t_no else f"img/{m_ref.group(1)}"

                        # 引用・レス先があれば80字以内で detail に記録
                        reply_target_detail: str | None = None
                        if effective_target_reply and isinstance(effective_target_reply, dict):
                            t_body = str(effective_target_reply.get("text") or "").strip().replace("\n", " ")
                            if t_body:
                                reply_target_detail = f"レス先: {t_body[:80]}"

                        await add_act_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            front="futaba",
                            kind="post",
                            summary=str(comment or "").strip()[:400],
                            detail=reply_target_detail,
                            guild_id=None,
                            channel_id=None,
                            source_ref=source_ref,
                        )
                    except Exception as act_e:
                        log.debug("failed to record futaba post activity: %s", act_e)

        if bool(result.get("success")):
            try:
                llm_emotion = await self._generate_hutaba_post_emotion_via_llm(
                    url=url,
                    thread=thread,
                    comment=comment,
                    target_reply=effective_target_reply,
                )
                emotion_state = getattr(self, "emotion_state", None)
                if emotion_state is not None and hasattr(emotion_state, "scores") and isinstance(emotion_state.scores, dict):
                    scores = emotion_state.scores
                    for k in list(scores.keys()):
                        scores[k] = round(float(scores.get(k, 0.0) or 0.0) * 0.75, 4)

                    if isinstance(llm_emotion, dict):
                        llm_scores = llm_emotion.get("scores")
                        if isinstance(llm_scores, dict) and llm_scores:
                            for k, v in llm_scores.items():
                                if k in scores or k in ("joy", "anticipation", "surprise", "trust", "sadness", "anger", "fear", "disgust"):
                                    scores[k] = min(1.0, round(float(scores.get(k, 0.0) or 0.0) + float(v) * 0.5, 4))
                        dom_llm = str(llm_emotion.get("dominant_emotion") or "joy")
                        if dom_llm != "neutral":
                            scores[dom_llm] = max(float(scores.get(dom_llm, 0.0) or 0.0), 0.35)
                        dominant = max(scores.items(), key=lambda x: float(x[1]))[0] if scores else dom_llm
                        if float(scores.get(dominant, 0.0) or 0.0) < 0.25:
                            dominant = "neutral"
                        emotion_state.dominant_emotion = dominant
                        emotion_state.reason = str(llm_emotion["reason"])
                        log.info("[hutaba] 書き込み後感情をLLMで更新: dominant=%s", dominant)
                    else:
                        scores["joy"] = min(1.0, round(float(scores.get("joy", 0.0) or 0.0) + 0.30, 4))
                        if "anticipation" in scores:
                            scores["anticipation"] = min(1.0, round(float(scores.get("anticipation", 0.0) or 0.0) + 0.20, 4))
                        elif "fun" in scores:
                            scores["fun"] = min(1.0, round(float(scores.get("fun", 0.0) or 0.0) + 0.20, 4))
                        dominant = max(scores.items(), key=lambda x: float(x[1]))[0] if scores else "joy"
                        if float(scores.get(dominant, 0.0) or 0.0) < 0.25:
                            dominant = "neutral"
                        emotion_state.dominant_emotion = dominant
                        emotion_state.reason = _build_hutaba_post_emotion_reason(
                            thread,
                            comment,
                            target_reply=effective_target_reply,
                            dominant_emotion=dominant,
                            url=url,
                        )

                    recompute_fn = getattr(self, "_recompute_composite_indices", None)
                    if callable(recompute_fn):
                        with contextlib.suppress(Exception):
                            recompute_fn()
                    save_fn = getattr(self, "_save_emotion_state", None)
                    if callable(save_fn):
                        save_fn()
                else:
                    update_emotion = getattr(self, "_update_emotion_state", None)
                    fallback_reason = (
                        str(llm_emotion["reason"])
                        if isinstance(llm_emotion, dict) and llm_emotion.get("reason")
                        else _build_hutaba_post_emotion_reason(
                            thread,
                            comment,
                            target_reply=effective_target_reply,
                            dominant_emotion="joy",
                            url=url,
                        )
                    )
                    changes_dict = (
                        dict(llm_emotion["scores"])
                        if isinstance(llm_emotion, dict) and isinstance(llm_emotion.get("scores"), dict) and llm_emotion["scores"]
                        else {"joy": 0.3, "fun": 0.2}
                    )
                    if callable(update_emotion):
                        try:
                            await update_emotion(
                                changes=changes_dict,
                                reason=fallback_reason,
                            )
                        except TypeError:
                            pass
                sync_presence = getattr(self, "_sync_emotion_presence", None)
                if callable(sync_presence):
                    await sync_presence(force=True)
            except Exception as e:
                log.debug("[hutaba] 投稿成功時の感情更新をスキップ: %s", e)
        else:
            log.warning(
                "[hutaba] スレッド書き込み失敗: url=%s err=%s comment=%r",
                url,
                result.get("error") or "unknown",
                comment,
            )

        if bool(result.get("success")) and not bool(result.get("dry_run")) and thread_state is not None:
            self._remember_img2chan_pending_own_post(thread_state, comment)
            known_before_after = set(thread_state.get("known_reply_post_nos", set()) or [])
            try:
                cached_caption = str(thread_state.get("op_image_caption") or (thread or {}).get("op_image_caption") or "").strip() or None
                try:
                    after_thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=self._img2chan_thread_monitor_max_replies(),
                        timeout_sec=timeout_sec,
                        cached_caption=cached_caption,
                    )
                except TypeError:
                    after_thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=self._img2chan_thread_monitor_max_replies(),
                        timeout_sec=timeout_sec,
                    )
            except Exception as e:
                log.warning("[hutaba] 投稿後スレッド再取得失敗 url=%s err=%r", url, e)
            else:
                if after_thread and isinstance(after_thread, dict):
                    caption = str(after_thread.get("op_image_caption") or "").strip()
                    if caption and not thread_state.get("op_image_caption"):
                        thread_state["op_image_caption"] = caption
                after_error = str((after_thread or {}).get("error") or "").strip()
                if not after_error:
                    matched_reply = getattr(helper, "find_img_thread_reply_by_comment")(
                        after_thread,
                        comment,
                        exclude_post_numbers=known_before_after,
                    ) if hasattr(helper, "find_img_thread_reply_by_comment") else None
                    self._img2chan_track_known_replies(thread_state, after_thread)
                    if matched_reply:
                        self._register_img2chan_own_post(thread_state, matched_reply)
                        result["registered_post_no"] = str(matched_reply.get("post_no") or "").strip()
                        log.info(
                            "[hutaba] スレッド書き込み反映確認: url=%s post_no=%s comment=%r",
                            url,
                            result["registered_post_no"],
                            comment,
                        )
                    else:
                        matched_replies = self._match_img2chan_pending_own_posts(thread_state, after_thread)
                        if matched_replies:
                            result["registered_post_no"] = str((matched_replies[-1] or {}).get("post_no") or "").strip()
                            log.info(
                                "[hutaba] スレッド保留書き込み反映確認: url=%s post_no=%s comment=%r",
                                url,
                                result["registered_post_no"],
                                comment,
                            )
            await self._store_img2chan_post_record(
                message,
                url=url,
                comment=comment,
                target_reply=target_reply,
                registered_post_no=str(result.get("registered_post_no") or "").strip(),
                dry_run=bool(result.get("dry_run")),
            )

        if report_result:
            await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
        return result

    async def _send_img2chan_post_report(
        self,
        message: discord.Message | None,
        post_result: dict[str, Any],
        *,
        delay_sec: float | None = None,
    ) -> None:
        if message is None:
            return
        if not cfg_bool("IMG2CHAN_POST_REPORT_ENABLED", False):
            return
        helper = _get_img2chan_helper()
        if helper is not None and hasattr(helper, "format_img_post_discord_report"):
            report = getattr(helper, "format_img_post_discord_report")(post_result, delay_sec=delay_sec)
        else:
            result = dict(post_result or {})
            report = str(result.get("error") or "imgの書き込み処理が終わりました。")
        report = truncate_text(str(report or "").strip(), 1900)
        if not report:
            return
        channel = getattr(message, "channel", None)
        if channel is None or not hasattr(channel, "send"):
            return
        await channel.send(report, allowed_mentions=discord.AllowedMentions.none())
