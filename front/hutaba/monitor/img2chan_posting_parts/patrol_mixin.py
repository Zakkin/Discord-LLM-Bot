"""自発スレ巡回・遅延投稿スケジュール・自律相槌・スレ監視ループ・引用返信タスク Mixin。"""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
from typing import Any

import discord

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _Img2chanPostingBase,
    _get_channel_kind,
    _get_img2chan_helper,
    author_id,
    cfg,
    cfg_bool,
    cfg_float,
    channel_id,
    content,
    log,
    normalize_img_thread_url,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _has_hutaba_unsupported_media,
)
from front.hutaba.monitor.img2chan_posting_parts.text_guards import (
    _looks_like_hutaba_anger_or_denial,
)


class _Img2chanPatrolMixin(_Img2chanPostingBase):
    async def _maybe_schedule_img2chan_thread_post(self, message: discord.Message) -> None:
        self._ensure_img2chan_proactive_post_task()
        if not cfg_bool("IMG2CHAN_AUTO_POST_ENABLED", True):
            return
        if message.guild is None:
            return
        if getattr(message.author, "bot", False):
            return
        if _get_channel_kind(message) == "none":
            return

        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "_extract_img_thread_urls_full"):
            from front.hutaba.client import img2chan as _img2chan_client
            helper = _img2chan_client
        if helper is None or not hasattr(helper, "_extract_img_thread_urls_full"):
            return

        urls = list(getattr(helper, "_extract_img_thread_urls_full")(content(message)) or [])
        if not urls:
            return

        # 複数指定時は確実に先頭の1件のみを採用
        if len(urls) > 1:
            log.info("[hutaba] 複数スレッドURLを検知。先頭の1件のみを監視対象にします: selected=%s total=%d", urls[0], len(urls))
            urls = urls[:1]

        self._prune_img2chan_post_schedules()
        tasks, recent, states = self._ensure_img2chan_thread_tracking_state()
        now = time.monotonic()
        active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()

        target_raw_url = urls[0]
        normalize = getattr(helper, "normalize_img_thread_url", lambda value: str(value or "").strip())
        url = str(normalize(target_raw_url) or "").strip()
        if not url:
            return

        # 既存アクティブタスクが自発巡回（proactive）の場合は中断してユーザー指定スレッドを最優先
        if active_url and active_url in tasks and not tasks[active_url].done():
            active_state = states.get(active_url, {})
            if not active_state.get("user_requested"):
                log.info("[hutaba] 自発巡回スレッドを中断し、ユーザー指定スレッドへ切り替え: old=%s new=%s", active_url, url)
                tasks[active_url].cancel()
                self._img2chan_active_thread_url = ""
                active_url = ""

        if (
            cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True)
            and active_url
            and url != active_url
            and active_url in tasks
            and not tasks[active_url].done()
        ):
            log.info("[hutaba] アクティブスレッド排他制御によりスキップ url=%s active_url=%s", url, active_url)
            return
        if url in tasks and not tasks[url].done():
            log.info("[hutaba] 投稿タスク既存のためスキップ url=%s", url)
            return

        # ユーザー指定スレッドの初回遅延は短縮（デフォルト5.0秒）
        delay_sec = max(cfg_float("IMG2CHAN_USER_REQUESTED_POST_DELAY_SEC", 5.0), 0.0)
        recent[url] = now
        thread_state = self._img2chan_thread_state(url)
        thread_state["origin_channel_id"] = channel_id(message)
        thread_state["origin_message_id"] = getattr(message, "id", None)
        thread_state["user_requested"] = True
        thread_state["focus_mode"] = True
        self._img2chan_active_thread_url = url
        task = asyncio.create_task(
            self._run_img2chan_thread_post_after_delay(message, url, delay_sec)
        )
        tasks[url] = task

        def _done_callback(done_task: asyncio.Task, *, scheduled_url: str = url) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    log.exception("[hutaba] スレッド投稿タスク例外発生 url=%s err=%r", scheduled_url, exc)
            task_map = getattr(self, "_img2chan_post_tasks", None)
            if isinstance(task_map, dict) and task_map.get(scheduled_url) is done_task:
                task_map.pop(scheduled_url, None)
            state_map = getattr(self, "_img2chan_thread_states", None)
            if isinstance(state_map, dict):
                state_map.pop(scheduled_url, None)
            active_thread_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
            if active_thread_url == scheduled_url:
                self._img2chan_active_thread_url = ""

        task.add_done_callback(_done_callback)
        log.info(
            "[hutaba] スレッド投稿スケジュール url=%s delay_sec=%.1f channel=%s author=%s",
            url,
            delay_sec,
            channel_id(message),
            author_id(message),
        )

    async def _run_img2chan_thread_post_after_delay(
        self,
        message: discord.Message | None,
        url: str,
        delay_sec: float,
    ) -> None:
        thread_state = self._img2chan_thread_state(url)
        try:
            await asyncio.sleep(max(float(delay_sec), 0.0))
            await self._yield_to_user_replies()
            post_res = await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=delay_sec,
                thread_state=thread_state,
                requester_text=content(message) if message is not None else "",
                report_result=True,
            )
            if isinstance(post_res, dict) and not (post_res.get("posted") or post_res.get("success")) and not thread_state.get("user_requested"):
                log.info(
                    "[hutaba] 自発スレッド初回投稿が見送られたため監視セッションを開始せず終了します: url=%s",
                    url,
                )
                return
            await self._monitor_img2chan_thread_session(message, url, thread_state=thread_state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[hutaba] 遅延投稿処理例外発生 url=%s err=%r", url, e)
            await self._send_img2chan_post_report(
                message,
                {
                    "success": False,
                    "url": url,
                    "comment": "",
                    "error": f"処理中にエラーが発生しました: {e}",
                },
                delay_sec=delay_sec,
            )
        finally:
            reply_tasks = thread_state.get("reply_tasks")
            if isinstance(reply_tasks, dict):
                running_tasks = [task for task in reply_tasks.values() if task is not None and not task.done()]
                for task in running_tasks:
                    task.cancel()
                if running_tasks:
                    await asyncio.gather(*running_tasks, return_exceptions=True)

    async def _select_proactive_thread_candidate(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """ペルソナや興味に基づいてカタログ候補から最適なスレッドを1件選定する。"""
        if not candidates:
            return None

        def _is_cryptic_high_velocity_candidate(cand: dict[str, Any]) -> bool:
            res_cnt = int(cand.get("res_count") or 0)
            if res_cnt < 100:
                return False
            raw_t = str(cand.get("title") or cand.get("op_text") or "").strip()
            core_t = re.sub(r"[\s\u3000、。，．・…‥!！?？「」『』（）()［］\[\]〜~ー―\-━ﾟ∀°]+", "", raw_t)
            return len(core_t) <= 8

        filtered_candidates = [c for c in candidates if not _is_cryptic_high_velocity_candidate(c)]
        effective_candidates = filtered_candidates if filtered_candidates else candidates

        if len(effective_candidates) == 1:
            return effective_candidates[0]

        core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()
        first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
        persona_context = f"{core_values}".strip()
        if first_person:
            persona_context = f"一人称: {first_person}\n{persona_context}".strip()

        candidate_summaries = []
        for idx, c in enumerate(effective_candidates[:8]):
            title = str(c.get("title") or c.get("text") or "(無題)")[:40]
            res_count = int(c.get("res_count") or 0)
            candidate_summaries.append(f"{idx + 1}. [レス数:{res_count}] {title}")

        board_name = str(effective_candidates[0].get("board") or "img")
        selection_prompt = (
            f"あなたは自分の興味や性格に最も合うふたば☆ちゃんねる({board_name})のスレッドを1つ選びます。\n"
            f"[あなたの人格・興味]\n{persona_context or '日常の雑談、趣味、面白い話題'}\n\n"
            f"[スレッド一覧]\n" + "\n".join(candidate_summaries) + "\n\n"
            "【選定指針】\n"
            "- タイトルやOPが短いセリフだけ（例:『私は先生のことを愛してます』等）でレス数が100件以上進んでいるスレッドは、特定ソシャゲ・版権の専門定時スレである可能性が高いため、自分がよく知らない作品・文脈が掴めないスレは避け、日常雑談や分かりやすいテーマのスレッドを優先して選ぶこと。\n\n"
            "最も参加したいスレッドの番号（1〜）のみを数字だけで答えてください。"
        )

        try:
            await self._yield_to_user_replies()
            model = str(cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MODEL", "")) or "").strip()
            if model:
                resp = await self._call_img2chan_ollama_json(
                    selection_prompt,
                    purpose="proactive_thread_selection",
                    model=model,
                    system_prompt="スレッド選定補助AI。JSONで selected_index: 整数 を返してください。",
                    schema={
                        "type": "object",
                        "properties": {"selected_index": {"type": "integer"}},
                        "required": ["selected_index"],
                    },
                    think=False,
                    temperature=0.3,
                    timeout_sec=15.0,
                )
                if isinstance(resp, dict):
                    idx = int(resp.get("selected_index", 1) or 1) - 1
                    if 0 <= idx < len(effective_candidates[:8]):
                        return effective_candidates[idx]
        except Exception as e:
            log.debug("[hutaba] 自発スレ選定LLM失敗、ランダムフォールバック: %r", e)

        return random.choice(effective_candidates[:min(5, len(effective_candidates))])

    def _select_hutaba_patrol_board(self) -> tuple[str, str]:
        """現在の感情状態（気分・好奇心・高揚感・疲労等）に基づいて巡回するふたばサーバー（'img' または 'may'）を決定する。"""
        boards_cfg = cfg("HUTABA_PATROL_BOARDS", "img,may").strip().lower()
        allow_img = "img" in boards_cfg
        allow_may = "may" in boards_cfg
        if allow_img and not allow_may:
            return ("img", "設定によりimg固定")
        if allow_may and not allow_img:
            return ("may", "設定によりmay固定")

        may_prob = cfg_float("HUTABA_MAY_PATROL_BASE_PROB", 0.50)
        reasons: list[str] = []

        emotion_state = getattr(self, "emotion_state", None)
        mood = "neutral"
        dominant = "neutral"
        if emotion_state is not None:
            mood = str(getattr(emotion_state, "mood", "neutral") or "neutral").strip().lower()
            dominant = str(getattr(emotion_state, "dominant_emotion", "neutral") or "neutral").strip().lower()
            raw_scores = getattr(emotion_state, "scores", None)
            scores = raw_scores if isinstance(raw_scores, dict) else {}

            curiosity = float(getattr(emotion_state, "curiosity", scores.get("anticipation", 0.0)) or 0.0)
            excitement = float(getattr(emotion_state, "excitement", scores.get("joy", 0.0)) or 0.0)
            boredom = float(getattr(emotion_state, "boredom", 0.0) or 0.0)
            fatigue = float(getattr(emotion_state, "fatigue", 0.0) or 0.0)
            tension = float(getattr(emotion_state, "tension", scores.get("fear", 0.0)) or 0.0)
            loneliness = float(getattr(emotion_state, "loneliness", scores.get("sadness", 0.0)) or 0.0)

            # 1. 総合ムード（mood）による傾斜
            if mood in ("excited", "upbeat", "curious", "restless", "playful", "energetic"):
                may_prob += 0.20
                reasons.append(f"気分({mood})で賑やか・雑多な話題を求めている")
            elif mood in ("fatigued", "gloomy", "tense", "calm", "sleepy", "melancholy"):
                may_prob -= 0.20
                reasons.append(f"気分({mood})で落ち着いたゆるい空気を求めている")

            # 2. 主要感情（dominant_emotion）による傾斜
            if dominant in ("anticipation", "surprise"):
                may_prob += 0.15
                reasons.append(f"感情({dominant})により刺激・新奇性志向")
            elif dominant == "joy" and excitement >= 0.45:
                may_prob += 0.10
                reasons.append("高揚感がありお祭り気分")
            elif dominant in ("trust", "sadness", "fear"):
                may_prob -= 0.15
                reasons.append(f"感情({dominant})により馴染みのまったり志向")
            elif dominant in ("anger", "disgust"):
                may_prob += 0.10
                reasons.append(f"感情({dominant})の発散に勢いのあるスレ志向")

            # 3. 複合感情インデックスによる傾斜
            if curiosity >= 0.55 or boredom >= 0.55:
                may_prob += 0.12
                reasons.append("好奇心・退屈しのぎに雑多なスレを開拓したい")
            if fatigue >= 0.50 or tension >= 0.50:
                may_prob -= 0.15
                reasons.append("少し疲れ・緊張がありゆるいスレで一息つきたい")
            if loneliness >= 0.55:
                may_prob += 0.08
                reasons.append("人恋しくて流速の速い場所を覗きたい")

        # 4. 連続同一サーバー巡回への飽き補正（気まぐれバランス）
        last_board = str(getattr(self, "_hutaba_last_patrol_board", "") or "").strip().lower()
        streak = int(getattr(self, "_hutaba_patrol_board_streak", 0) or 0)
        if streak >= 2:
            if last_board == "may":
                may_prob -= min(0.25, 0.10 * streak)
                reasons.append("mayが続いたので気分転換も意識")
            elif last_board == "img":
                may_prob += min(0.25, 0.10 * streak)
                reasons.append("imgが続いたので気分転換も意識")

        may_prob = max(0.15, min(0.85, round(may_prob, 3)))
        selected = "may" if random.random() < may_prob else "img"

        setattr(self, "_hutaba_last_patrol_board", selected)
        setattr(self, "_hutaba_patrol_board_streak", (streak + 1) if selected == last_board else 1)

        reason_summary = "・".join(reasons[:2]) if reasons else ("気まぐれで賑やかなmayへ" if selected == "may" else "気まぐれでまったりなimgへ")
        full_reason = f"mood={mood}/dominant={dominant} ({reason_summary}) [may_prob={may_prob:.2f}]"
        return (selected, full_reason)

    def _ensure_img2chan_proactive_post_task(self) -> None:
        """自発スレ巡回ループタスクが未起動なら起動する。"""
        if not self._img2chan_proactive_post_enabled():
            return
        task_post = getattr(self, "_img2chan_proactive_post_task", None)
        if task_post is not None and not task_post.done():
            self._img2chan_proactive_task = task_post
            return
        task_pro = getattr(self, "_img2chan_proactive_task", None)
        if task_pro is not None and not task_pro.done():
            self._img2chan_proactive_post_task = task_pro
            return
        new_task = asyncio.create_task(self._img2chan_proactive_post_loop())
        self._img2chan_proactive_post_task = new_task
        self._img2chan_proactive_task = new_task
        log.info("[hutaba] 自発スレ巡回ループタスクを起動しました")

    async def _img2chan_proactive_post_loop(self) -> None:
        """ふたば（img / may）カタログから定期的にスレッドを自発選定して書き込むループ。"""
        log.info("[hutaba] 自発スレ巡回ループを開始しました")
        initial_delay = max(cfg_float("HUTABA_PROACTIVE_POST_INITIAL_DELAY_SEC", 120.0), 5.0)
        await asyncio.sleep(initial_delay)

        while True:
            try:
                await self._yield_to_user_replies()

                if not self._img2chan_proactive_post_enabled():
                    await asyncio.sleep(60.0)
                    continue

                active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
                tasks, recent, states = self._ensure_img2chan_thread_tracking_state()
                active_task = tasks.get(active_url) if active_url else None
                active_state = states.get(active_url, {}) if active_url else {}

                if (
                    (cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True) and active_url and active_task and not active_task.done())
                    or (bool(active_state.get("user_requested")) and active_task and not active_task.done())
                ):
                    log.debug("[hutaba] 既存スレッド監視中（user_requested=%s）のため自発選定を待機: active_url=%s", active_state.get("user_requested", False), active_url)
                    await asyncio.sleep(120.0)
                    continue

                helper = _get_img2chan_helper()
                fetch_catalog = getattr(helper, "fetch_img_top5_threads", None) if helper is not None else None
                if not callable(fetch_catalog):
                    from front.hutaba.client import img2chan as _img2chan_client
                    fetch_catalog = getattr(_img2chan_client, "fetch_img_top5_threads", None)

                if not callable(fetch_catalog):
                    log.warning("[hutaba] カタログ取得関数が利用できないため自発投稿をスキップします")
                    await asyncio.sleep(60.0)
                    continue

                selected_board, board_reason = self._select_hutaba_patrol_board()
                log.info(
                    "[hutaba] 気分に基づく巡回サーバー選定: board=%s (%s)",
                    selected_board,
                    board_reason,
                )

                fallback_board = "img" if selected_board == "may" else "may"
                boards_to_try = [selected_board, fallback_board]
                candidates: list[dict[str, Any]] = []
                active_patrol_board = selected_board

                _, recent, states = self._ensure_img2chan_thread_tracking_state()
                now_mono = time.monotonic()
                dedup_ttl = max(cfg_float("IMG2CHAN_AUTO_POST_DEDUP_TTL_SEC", 3600.0), 60.0)

                for board_idx, current_board in enumerate(boards_to_try):
                    if board_idx > 0:
                        log.info(
                            "[hutaba] %s で有効な候補スレッドが見つからなかったため %s へ巡回先をフォールバックします",
                            selected_board,
                            current_board,
                        )
                    try:
                        catalog_threads = await fetch_catalog(limit=15, timeout_sec=20.0, server=current_board)
                    except TypeError:
                        if board_idx > 0:
                            break
                        catalog_threads = await fetch_catalog(limit=15, timeout_sec=20.0)

                    if not catalog_threads or not isinstance(catalog_threads, list):
                        log.info("[hutaba] カタログ取得結果が空でした (board=%s)", current_board)
                        continue

                    board_candidates: list[dict[str, Any]] = []
                    for item in catalog_threads:
                        if not isinstance(item, dict):
                            continue
                        raw_url = str(item.get("url") or "").strip()
                        t_url = normalize_img_thread_url(raw_url)
                        if not t_url:
                            continue
                        if self._is_img2chan_thread_vandalized(t_url):
                            continue
                        stream_reason = self._is_img2chan_stream_or_live_thread(item)
                        if stream_reason:
                            log.info(
                                "[hutaba] 配信・実況系スレッドのため自発投稿対象から除外: %s (reason=%s title=%s)",
                                t_url,
                                stream_reason,
                                str(item.get("title") or "")[:40],
                            )
                            continue
                        request_reason = self._is_img2chan_request_thread(item)
                        if request_reason:
                            log.info(
                                "[hutaba] リクエスト系スレッドのため自発投稿対象から除外: %s (reason=%s title=%s)",
                                t_url,
                                request_reason,
                                str(item.get("title") or item.get("text") or "")[:40],
                            )
                            continue
                        expire_reason = self._is_hutaba_thread_expiring_soon(item)
                        if expire_reason:
                            log.info(
                                "[hutaba] 消滅間近スレッド（10分以内等）のため自発投稿対象から除外: %s (reason=%s expire_text=%s title=%s)",
                                t_url,
                                expire_reason,
                                str(item.get("expire_text") or ""),
                                str(item.get("title") or "")[:40],
                            )
                            continue
                        res_count = int(item.get("res_count") or item.get("reply_count") or 0)
                        max_proactive_res = int(cfg("HUTABA_MAX_PROACTIVE_THREAD_RES_COUNT", 650) or 650)
                        if res_count > max_proactive_res:
                            log.debug(
                                "[hutaba] 終盤スレッド（レス数:%d > %d）のため自発投稿対象から除外: %s",
                                res_count,
                                max_proactive_res,
                                t_url,
                            )
                            continue
                        if res_count > 850:
                            continue
                        text = str(item.get("text") or "").strip()
                        if not text or text in {"(取得失敗)", "(エラー)"}:
                            continue
                        last_posted = float(recent.get(t_url, 0.0) or 0.0)
                        if last_posted and (now_mono - last_posted) < dedup_ttl:
                            continue
                        st = states.get(t_url)
                        if isinstance(st, dict) and st.get("status") in ("aborted", "completed"):
                            continue
                        item_copy = dict(item)
                        item_copy["normalized_url"] = t_url
                        item_copy["board"] = str(item.get("board") or current_board)
                        board_candidates.append(item_copy)

                    if board_candidates:
                        candidates = board_candidates
                        active_patrol_board = current_board
                        break

                if not candidates:
                    log.info("[hutaba] 条件を満たす新規巡回候補スレッドがありませんでした")
                    continue

                log.info(
                    "[hutaba] 自発スレッド選定開始: 候補数=%d (board=%s)",
                    len(candidates),
                    active_patrol_board,
                )
                await self._wait_for_discord_user_replies_idle()
                selected = await self._select_proactive_thread_candidate(candidates)
                if selected:
                    selected_url = str(selected.get("normalized_url") or selected.get("url") or "").strip()
                    selected_title = str(selected.get("title") or selected.get("text") or "")[:30]
                    log.info("[hutaba] 自発スレッド選定完了: url=%s title=%s", selected_url, selected_title)

                    recent[selected_url] = now_mono
                    thread_state = self._img2chan_thread_state(selected_url)
                    thread_state["board"] = active_patrol_board
                    self._img2chan_active_thread_url = selected_url

                    task = asyncio.create_task(
                        self._run_img2chan_thread_post_after_delay(None, selected_url, delay_sec=0.0)
                    )
                    tasks[selected_url] = task

                    def _done_cb(done_task: asyncio.Task, *, s_url: str = selected_url) -> None:
                        with contextlib.suppress(asyncio.CancelledError):
                            exc = done_task.exception()
                            if exc is not None:
                                log.exception("[hutaba] 自発投稿タスク例外発生 url=%s err=%r", s_url, exc)
                        task_map = getattr(self, "_img2chan_post_tasks", None)
                        if isinstance(task_map, dict) and task_map.get(s_url) is done_task:
                            task_map.pop(s_url, None)
                        if getattr(self, "_img2chan_active_thread_url", "") == s_url:
                            self._img2chan_active_thread_url = ""

                    task.add_done_callback(_done_cb)

                interval_min = self._img2chan_proactive_post_interval_min_sec()
                interval_max = self._img2chan_proactive_post_interval_max_sec()
                sleep_duration = random.uniform(interval_min, interval_max)
                log.info("[hutaba] 次の自発巡回まで待機: %.1f秒", sleep_duration)
                await asyncio.sleep(sleep_duration)

            except asyncio.CancelledError:
                log.info("[hutaba] 自発スレ巡回ループがキャンセルされました")
                raise
            except Exception as e:
                log.exception("[hutaba] 自発スレ巡回ループでエラー発生: %r", e)
                await asyncio.sleep(60.0)

    def _select_img2chan_chitchat_target(
        self,
        thread: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """相槌・追撃レスの対象として、既に他者から返信をもらっているレスを最優先で選定する。"""
        if not candidates:
            return None

        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        # 既に誰かから返信をもらっているレス（被引用数 > 0）を抽出
        replied_candidates: list[tuple[int, int, dict[str, Any]]] = []
        for idx, cand in enumerate(candidates):
            if _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                continue
            p_no = str(cand.get("post_no") or "").strip()
            r_num = str(int(cand.get("number") or 0))
            raw_cnt = reply_counts.get(p_no, 0) or reply_counts.get(r_num, 0)
            cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) else 0
            if cnt > 0:
                # 被返信数降順、かつ同数なら新着順（idx昇順）
                replied_candidates.append((-cnt, idx, cand))

        if replied_candidates:
            replied_candidates.sort(key=lambda x: (x[0], x[1]))
            selected = replied_candidates[0][2]
            log.info(
                "[hutaba] 既に返信されているホットなレスを相槌対象に選定: post_no=%s quoted_count=%d",
                selected.get("post_no"),
                -replied_candidates[0][0],
            )
            return selected

        # 返信をもらっているレスがない場合は最新レスにフォールバック（動画レスは除外）
        for cand in candidates:
            if not _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                return cand
        return None

    async def _maybe_schedule_img2chan_autonomous_chitchat(
        self,
        message: discord.Message | None,
        url: str,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> None:
        """引用返信がなくても、他人の会話が新しく進行している場合にのみ相槌・追撃レスを検討する。"""
        if not cfg_bool("HUTABA_AUTONOMOUS_CHITCHAT_ENABLED", False):
            # デフォルトは無効または慎重運用とし、不要な自己連投リスクを遮断
            return

        now = time.time()
        last_own_post = float(thread_state.get("last_own_post_at", 0.0) or 0.0)
        if thread_state.get("user_requested"):
            cooldown = max(cfg_float("IMG2CHAN_USER_REQUESTED_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC", 120.0), 10.0)
        else:
            cooldown = self._img2chan_autonomous_followup_cooldown_sec()
        if last_own_post > 0 and (now - last_own_post) < cooldown:
            return

        replies = list((thread or {}).get("replies") or [])
        if len(replies) < 3:
            return

        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set(thread_state.get("own_post_texts", {}).values())

        # 【超重要ガード1】スレッドの最新レス（末尾）が自分自身の場合は絶対に連投しない
        latest_reply = replies[-1]
        latest_post_no = str(latest_reply.get("post_no") or "").strip()
        latest_text = str(latest_reply.get("text") or "").strip()
        if latest_post_no in own_nos or latest_text in own_texts:
            log.debug("[hutaba] スレ最新レスが自身のため追撃を抑止（他人のレス待ち）: url=%s", url)
            return

        # 【超重要ガード2】最後の自身の投稿より後に投稿された他人のレスのみを対象とする
        new_other_replies = []
        found_own_post = False
        for r in reversed(replies):
            p_no = str(r.get("post_no") or "").strip()
            p_txt = str(r.get("text") or "").strip()
            if p_no in own_nos or p_txt in own_texts:
                found_own_post = True
                break
            new_other_replies.append(r)

        # 自身の書き込み履歴があるのに、その後に他人の新着レスが1件もない場合は即座にリターン
        if found_own_post and not new_other_replies:
            return

        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        # 既に誰かの返信がついているレス（被引用数 > 0）のみを抽出
        candidate_pool = new_other_replies if new_other_replies else [replies[-1]]
        replied_candidates: list[dict[str, Any]] = []
        for cand in candidate_pool:
            if _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                continue
            p_no = str(cand.get("post_no") or "").strip()
            r_num = str(int(cand.get("number") or 0))
            if reply_counts.get(p_no, 0) > 0 or reply_counts.get(r_num, 0) > 0:
                replied_candidates.append(cand)

        # 誰の返信もついていないレスしかない場合は、特定レスへの返信は行わない（スキップ）
        if not replied_candidates:
            log.debug("[hutaba] 既に返信がついているレスが存在しないため相槌タスクを見送り: url=%s", url)
            return

        target_reply = self._select_img2chan_chitchat_target(thread, replied_candidates) or replied_candidates[0]
        if target_reply is None:
            return

        target_post_no = str(target_reply.get("post_no") or "").strip()
        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight

        if not target_post_no or target_post_no in own_nos or target_post_no in handled or target_post_no in inflight:
            return

        reply_tasks = thread_state.get("reply_tasks")
        if isinstance(reply_tasks, dict) and len(reply_tasks) == 0:
            inflight.add(target_post_no)
            reply_tasks[target_post_no] = asyncio.create_task(
                self._run_img2chan_followup_reply_task(
                    message,
                    url,
                    thread_state=thread_state,
                    target_reply=target_reply,
                    quoted_own_posts=[],
                    target_replies=replies,
                )
            )
            log.info("[hutaba] スレ進行に応じた自発的相槌タスクをスケジュール url=%s target_post_no=%s", url, target_post_no)

    async def _monitor_img2chan_thread_session(
        self,
        message: discord.Message | None,
        url: str,
        *,
        thread_state: dict[str, Any],
    ) -> None:
        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "fetch_img_thread"):
            return
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        max_replies = self._img2chan_thread_monitor_max_replies()
        if thread_state.get("user_requested"):
            poll_sec = max(cfg_float("IMG2CHAN_USER_REQUESTED_POLL_SEC", 15.0), 5.0)
        else:
            poll_sec = self._img2chan_thread_monitor_poll_sec()

        while True:
            try:
                await self._yield_to_user_replies()
                cached_caption = str(thread_state.get("op_image_caption") or "").strip() or None
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
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[hutaba] スレッド監視フェッチ失敗 url=%s err=%r", url, e)
                await asyncio.sleep(poll_sec)
                continue

            error = str((thread or {}).get("error") or "").strip()
            if error:
                if self._img2chan_thread_dropped(error):
                    thread_state["status"] = "completed"
                    log.info("[hutaba] スレッド完走・消滅を検知（セッション正常終了）: url=%s reason=%s", url, error)
                    if getattr(self, "_img2chan_active_thread_url", "") == url:
                        self._img2chan_active_thread_url = ""
                    return
                log.warning("[hutaba] スレッド監視一時的取得エラー url=%s err=%s", url, error)
                await asyncio.sleep(poll_sec)
                continue

            if thread and isinstance(thread, dict):
                caption = str(thread.get("op_image_caption") or "").strip()
                if caption and not thread_state.get("op_image_caption"):
                    thread_state["op_image_caption"] = caption

            # 荒らし・不毛スレ検知（Vandalism Detection）による早期撤退
            vandalism_reason = self._detect_img2chan_thread_vandalism(thread, thread_state)
            if vandalism_reason:
                thread_state["status"] = "aborted"
                thread_state["abort_reason"] = f"vandalism_detected: {vandalism_reason}"
                log.warning(
                    "[hutaba] 荒らし・不毛スレ化を検知したため書き込み・監視を中止（撤退）: url=%s reason=%s",
                    url,
                    vandalism_reason,
                )
                self._mark_img2chan_thread_vandalized(url, vandalism_reason)
                reply_tasks = thread_state.get("reply_tasks")
                if isinstance(reply_tasks, dict):
                    for task in list(reply_tasks.values()):
                        if task is not None and not task.done():
                            task.cancel()
                if getattr(self, "_img2chan_active_thread_url", "") == url:
                    self._img2chan_active_thread_url = ""
                return

            self._match_img2chan_pending_own_posts(thread_state, thread)
            thread_state["target_replies"] = list((thread or {}).get("replies") or [])
            candidates = self._discover_img2chan_quote_reply_candidates(thread_state, thread)
            if candidates:
                self._schedule_img2chan_followup_reply_tasks(message, url, thread_state, candidates)
            else:
                await self._maybe_schedule_img2chan_autonomous_chitchat(message, url, thread_state, thread)

            await self._maybe_learn_from_img2chan_thread_feedback(message, url, thread_state, thread)
            await asyncio.sleep(poll_sec)

    async def _run_img2chan_followup_reply_task(
        self,
        message: discord.Message | None,
        url: str,
        *,
        thread_state: dict[str, Any],
        target_reply: dict[str, Any],
        quoted_own_posts: list[dict[str, Any]],
        target_replies: list[dict[str, Any]] | None = None,
    ) -> None:
        target_post_no = str(target_reply.get("post_no") or "").strip()
        target_text = str(target_reply.get("text") or "").strip()
        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set(thread_state.get("own_post_texts", {}).values())

        # 自分自身のレスへの返信は絶対に実行しない
        if target_post_no in own_nos or target_text in own_texts:
            log.warning("[hutaba] 自身のレスへの自己返信を検知したためタスクを破棄: url=%s target_post_no=%s", url, target_post_no)
            return

        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight
        raw_tasks = thread_state.get("reply_tasks")
        reply_tasks: dict[Any, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
        thread_state["reply_tasks"] = reply_tasks

        try:
            # 直近数件のレスを検査
            if target_replies is None:
                raw_target_replies = thread_state.get("target_replies")
                if isinstance(raw_target_replies, list):
                    target_replies = raw_target_replies
                else:
                    target_replies = [target_reply] if target_reply else []

            recent_comments = [str(r.get("com", "") or r.get("text", "")) for r in target_replies[-5:]]
            denial_hits = sum(1 for c in recent_comments if _looks_like_hutaba_anger_or_denial(c))
            if denial_hits >= 2:
                log.info(
                    "[HUTABA_FRUSTRATION] Denial/anger detected in thread %s (hits=%d). Skipping reply cycle for cooldown.",
                    url,
                    denial_hits,
                )
                return

            # 中断・再開用のチェックポイント保存
            self._save_img2chan_checkpoint(
                thread_state,
                target_post_no=target_post_no,
                action="followup_reply",
                url=url,
            )

            # Discordユーザー返信最優先
            await self._yield_to_user_replies()

            # 中断から復帰したか確認
            checkpoint = self._restore_img2chan_checkpoint(thread_state)
            if checkpoint and checkpoint.get("target_post_no") == target_post_no:
                log.info(
                    "[hutaba] Discord返信完了を検知。中断されていたスレ書き込み処理を再開: url=%s target=%s",
                    url,
                    target_post_no,
                )
                self._clear_img2chan_checkpoint(thread_state)

            result = await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=None,
                thread_state=thread_state,
                requester_text="",
                target_reply=target_reply,
                quoted_own_posts=quoted_own_posts,
                report_result=True,
            )
            if bool(result.get("success")):
                handled.add(target_post_no)
                thread_state["last_own_post_at"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[hutaba] 引用返信タスク例外発生 url=%s target_post_no=%s err=%r", url, target_post_no, e)
        finally:
            inflight.discard(target_post_no)
            if reply_tasks.get(target_post_no) is asyncio.current_task():
                reply_tasks.pop(target_post_no, None)
