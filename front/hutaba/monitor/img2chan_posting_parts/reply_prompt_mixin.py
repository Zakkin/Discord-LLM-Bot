"""ふたばレス生成前のスレッド文脈・記憶・添付画像キャプション・プロンプト構築 Mixin。"""
from __future__ import annotations

from collections import Counter
import inspect
import re
from typing import Any

import discord

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _HutabaReplyPromptContext,
    _IMG_THREAD_URL_PATTERN,
    _Img2chanPostingBase,
    _build_video_caution_note,
    _cfg_persona_namespace,
    _is_quoted_image_priority_enabled,
    _is_video_caution_enabled,
    _order_context_image_candidates,
    cfg,
    cfg_bool,
    content,
    discussed_topics,
    format_slang_guidance_for_prompt,
    get_relevant_memories,
    lessons,
    log,
    lookup_slang_in_text,
    sanitize_memory_candidates,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _collect_hutaba_prompt_context_replies,
    _extract_hutaba_attached_image_urls,
    _extract_hutaba_named_terms,
    _extract_hutaba_post_no_key,
    _has_hutaba_unsupported_media,
    _is_hutaba_context_image_caption_enabled,
    _resolve_hutaba_caption_fn,
)


class _Img2chanReplyPromptMixin(_Img2chanPostingBase):
    async def _prepare_hutaba_reply_prompt_context(
        self,
        message: discord.Message | None,
        url: str,
        *,
        helper: Any,
        max_chars: int,
        character_guidance: str,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        requester_text: str = "",
    ) -> _HutabaReplyPromptContext:
        """スレッド本文・レス履歴・長期記憶・事前調査・添付画像キャプションから生成プロンプトを構築する。"""
        # スレッド全投稿リスト（OP + 最序盤レス + 取得レス）を確保
        posts = list(thread.get("posts") or [])
        if not posts:
            op_post_no = str(thread.get("op_post_no") or "").strip()
            op_text = str(thread.get("op_text") or "").strip()
            op_caption = str(thread.get("op_image_caption") or "").strip()
            if op_caption:
                op_text = f"{op_text}\n[スレ画内容: {op_caption}]" if op_text else f"[スレ画内容: {op_caption}]"
            if op_post_no or op_text:
                posts.append({
                    "post_no": op_post_no,
                    "number": 0,
                    "text": op_text,
                    "is_op": True,
                })
            seen_keys: set[tuple[str, int]] = set()
            replies_source = list(thread.get("all_replies") or [*list(thread.get("early_replies") or []), *list(thread.get("replies") or [])])
            for reply in replies_source:
                if isinstance(reply, dict):
                    p_no = str(reply.get("post_no") or "").strip()
                    r_no = int(reply.get("number") or 0)
                    key = (p_no, r_no)
                    if (p_no or r_no > 0) and key in seen_keys:
                        continue
                    seen_keys.add(key)
                    posts.append({
                        "post_no": p_no,
                        "number": r_no,
                        "text": str(reply.get("text") or "").strip(),
                        "soudane": int(reply.get("soudane") or 0),
                        "is_op": False,
                    })

        # スレ全体からの固有名詞・共有語彙の抽出および定型カタカナ用語頻度表の構築
        thread_terms: set[str] = set()
        frequent_terms_counter: Counter[str] = Counter()
        _katakana_compound_re = re.compile(r"[\u30A1-\u30FA\u30FC]{4,}")

        replies_source_list = list(thread.get("all_replies") or [*list(thread.get("early_replies") or []), *list(thread.get("replies") or [])])
        all_posts_source = list(posts or []) + list(replies_source_list)
        for p in all_posts_source:
            if isinstance(p, dict):
                p_text = str(p.get("text") or p.get("comment") or "")
                if p_text:
                    for term in _extract_hutaba_named_terms(p_text):
                        thread_terms.add(term)
                    for km in _katakana_compound_re.finditer(p_text):
                        frequent_terms_counter[km.group(0)] += 1

        frequent_terms: dict[str, int] = {
            term: count for term, count in frequent_terms_counter.items() if count >= 5
        }

        # スレッドタイトルやOP本文の固有名詞も確実にカバー
        thread_title_raw = str(thread.get("title") or "").strip()
        if thread_title_raw:
            for term in _extract_hutaba_named_terms(thread_title_raw):
                thread_terms.add(term)

        # ペルソナ表示名・自称・別名の登録（誤検出防止）
        persona_names: set[str] = set()
        bot_name = str(cfg("BOT_NAME", "コイト") or "コイト").strip()
        if bot_name:
            thread_terms.add(bot_name)
            persona_names.add(bot_name)
        persona_names.update({"私", "あたし", "僕", "俺", "自分", "うち", "コイト"})

        # 板スラング辞典の見出し語を追加
        if lookup_slang_in_text is not None:
            # スラング辞書の見出し語を許可集合へマージ
            try:
                from front.hutaba.client.img2channet import _SLANG_DICTIONARY  # type: ignore[attr-defined]
                thread_terms.update(_SLANG_DICTIONARY.keys())
            except Exception:
                pass

        early_lines = self._extract_early_thread_context_lines(posts)

        # 記憶（memory.db）およびチャンネル要約の取得
        memory_lines: list[str] = []
        channel_summary: str = ""
        memory_store = getattr(self, "memory_store", None)

        # 直前の自分の投稿本文を取得（エコー汚染防止用）
        last_own_post_text = ""
        if quoted_own_posts:
            last_own_post_text = str(quoted_own_posts[0].get("text") or "").strip()
        if not last_own_post_text and isinstance(thread_state.get("own_post_texts"), dict):
            own_texts = list(thread_state["own_post_texts"].values())
            if own_texts:
                last_own_post_text = str(own_texts[-1] or "").strip()

        # スレッドIDの抽出（channel_id として使用）
        thread_id_str = str(thread.get("op_post_no") or "").strip()
        if not thread_id_str:
            m = _IMG_THREAD_URL_PATTERN.search(url)
            if m:
                thread_id_str = m.group(1)
        channel_id_val = int(thread_id_str) if thread_id_str.isdigit() else None

        # クエリテキストの生成（対象レス本文、または直近3レスとスレタイ）
        thread_title = str(thread.get("title") or thread.get("op_text") or "").strip()[:80]
        if target_reply and str(target_reply.get("text") or "").strip():
            target_body = str(target_reply.get("text") or "").strip()
            query_text = f"{thread_title} {target_body}".strip()
        else:
            recent_texts = [
                str(p.get("text") or "").strip()
                for p in posts[-3:]
                if str(p.get("text") or "").strip()
            ]
            recent_combined = " ".join(recent_texts)
            query_text = f"{thread_title} {recent_combined}".strip()

        if memory_store is not None and callable(get_relevant_memories):
            try:
                raw_memories, summary_res = await get_relevant_memories(
                    cfg=cfg,
                    store=memory_store,
                    guild_id=None,
                    channel_id=channel_id_val,
                    user_id=None,
                    user_text=query_text,
                )
                if summary_res:
                    channel_summary = str(summary_res).strip()

                # スレッド単体の記憶が0件の場合はふたば全体・過去学習から再検索（最大3件）
                if not raw_memories and channel_id_val is not None:
                    raw_global, _ = await get_relevant_memories(
                        cfg=cfg,
                        store=memory_store,
                        guild_id=None,
                        channel_id=None,
                        user_id=None,
                        user_text=query_text,
                    )
                    raw_memories = list(raw_global or [])[:3]

                memory_lines = sanitize_memory_candidates(list(raw_memories or []), last_assistant_text=last_own_post_text)
            except Exception as me:
                log.warning("[hutaba] memory retrieval exception url=%s: %r", url, me)
                memory_lines = []

        topic_research_note = await self._resolve_hutaba_thread_topic_context(
            thread,
            thread_state,
            target_reply=target_reply,
            posts=posts,
        )
        if topic_research_note:
            channel_summary = f"{channel_summary}\n\n{topic_research_note}".strip()

        # 対象レス本文からスラングを検出し、プロンプト注記を構築
        slang_guidance_lines: list[str] = []
        if target_reply and str(target_reply.get("text") or "").strip():
            matched_slangs = lookup_slang_in_text(str(target_reply.get("text") or ""))
            slang_guidance_lines = format_slang_guidance_for_prompt(matched_slangs)
        if slang_guidance_lines:
            slang_note = "【スレッド用語・スラング注意メモ】\n" + "\n".join(slang_guidance_lines)
            channel_summary = f"{channel_summary}\n\n{slang_note}".strip()

        target_context_lines: list[str] = []
        if target_reply is not None:
            target_context_lines = self._extract_hutaba_target_context_lines(
                thread, target_reply, posts
            )

        # 対象レスにあぷ小等の添付画像が存在する場合、Visionモデルで内容を認識してプロンプトへ注入
        attached_image_notes: list[str] = []
        attached_image_captions: list[str] = []
        if target_reply and str(target_reply.get("text") or "").strip():
            img_urls = _extract_hutaba_attached_image_urls(str(target_reply.get("text") or ""))
            caption_fn = _resolve_hutaba_caption_fn(helper) if img_urls else None
            if img_urls and callable(caption_fn):
                for img_url in img_urls[:2]:  # 最大2枚まで
                    try:
                        cap = await caption_fn(img_url)
                        if cap:
                            attached_image_captions.append(str(cap).strip())
                            attached_image_notes.append(f"- 添付画像 ({img_url.split('/')[-1]}): {cap}")
                    except Exception as e:
                        log.debug("[hutaba] 添付画像キャプション取得失敗 url=%s err=%r", img_url, e)

        attached_image_description = "\n".join(c for c in attached_image_captions if c).strip()
        if attached_image_notes:
            img_section = "【対象レスの添付画像（あぷ小）の解釈メモ】\n" + "\n".join(attached_image_notes)
            channel_summary = f"{channel_summary}\n\n{img_section}".strip()

        # 対象レスに動画が添付されている場合、視聴できない旨の注意を注入する
        if (
            target_reply
            and _has_hutaba_unsupported_media(str(target_reply.get("text") or ""))
            and _build_video_caution_note is not None
            and _is_video_caution_enabled is not None
            and _is_video_caution_enabled()
        ):
            channel_summary = f"{channel_summary}\n\n{_build_video_caution_note()}".strip()
            log.info(
                "[hutaba] video caution note injected: post_no=%s",
                _extract_hutaba_post_no_key(target_reply),
            )

        # 周辺レス（対象レス・OP以外）の添付画像を最新1件・1枚に限りキャプションして文脈解釈メモとして注入する
        if not attached_image_notes and _is_hutaba_context_image_caption_enabled():
            try:
                context_replies = _collect_hutaba_prompt_context_replies(
                    thread=thread,
                    posts=posts,
                    target_reply=target_reply,
                    early_lines=early_lines,
                    target_context_lines=target_context_lines,
                    helper=helper,
                )
                ordered_replies = context_replies
                quoted_keys: list[str] = []
                if (
                    _order_context_image_candidates is not None
                    and _is_quoted_image_priority_enabled is not None
                    and _is_quoted_image_priority_enabled()
                    and target_reply is not None
                ):
                    find_quoted_fn = getattr(helper, "find_img_thread_quoted_posts", None)
                    if callable(find_quoted_fn):
                        try:
                            quoted_posts = find_quoted_fn(thread, target_reply, max_targets=4) or []
                        except Exception:
                            quoted_posts = []
                        for qp in quoted_posts:
                            qk = _extract_hutaba_post_no_key(qp)
                            if qk:
                                quoted_keys.append(qk)
                    if quoted_keys:
                        ordered_replies = _order_context_image_candidates(
                            context_replies,
                            quoted_keys,
                            key_fn=_extract_hutaba_post_no_key,
                        )
                context_image_note = ""
                for cand in ordered_replies:
                    cand_img_urls = _extract_hutaba_attached_image_urls(str(cand.get("text") or ""))
                    if not cand_img_urls:
                        continue
                    cand_post_no = _extract_hutaba_post_no_key(cand)
                    if not cand_post_no:
                        continue
                    first_img_url = cand_img_urls[0]
                    filename = first_img_url.split("/")[-1]
                    caption_cache = None
                    if isinstance(thread_state, dict):
                        raw_cache = thread_state.get("res_image_captions")
                        if not isinstance(raw_cache, dict):
                            raw_cache = {}
                            thread_state["res_image_captions"] = raw_cache
                        caption_cache = raw_cache
                    cap_text = ""
                    if caption_cache is not None and cand_post_no in caption_cache:
                        cap_text = str(caption_cache.get(cand_post_no) or "").strip()
                    else:
                        cand_caption_fn = _resolve_hutaba_caption_fn(helper)
                        if callable(cand_caption_fn):
                            try:
                                raw_cap = await cand_caption_fn(first_img_url)
                                cap_text = str(raw_cap or "").strip()
                            except Exception as e:
                                log.debug(
                                    "[hutaba] 周辺レス添付画像キャプション取得失敗 post_no=%s url=%s err=%r",
                                    cand_post_no,
                                    first_img_url,
                                    e,
                                )
                            if cap_text and caption_cache is not None:
                                caption_cache[cand_post_no] = cap_text
                    if cap_text:
                        context_image_note = f"- No.{cand_post_no} の添付画像 ({filename}): {cap_text}"
                        log.info(
                            "[hutaba] 周辺レス画像キャプション選定 post_no=%s reason=%s",
                            cand_post_no,
                            "quoted" if cand_post_no in quoted_keys else "latest",
                        )
                    # 1回の生成につき対象は最も新しい画像つき周辺レス1件（画像1枚）のみ
                    break

                if context_image_note:
                    sibling_img_section = (
                        "【周辺レスの添付画像（あぷ小）の解釈メモ】\n"
                        + context_image_note
                    )
                    base_summary = str(channel_summary or "").strip()
                    channel_summary = (
                        f"{base_summary}\n\n{sibling_img_section}".strip()
                        if base_summary
                        else sibling_img_section
                    )
            except Exception as e:
                log.debug("[hutaba] 周辺レス添付画像処理失敗 err=%r", e)

        macro_topic_summary = self._extract_img2chan_macro_topic_summary(thread, posts)

        # 匿名ユーザー（としあき）対応: 固定author_id、個人習慣プロファイルや好感度はバイパス
        prompt = ""
        if target_reply is not None and helper is not None and hasattr(helper, "build_img_thread_followup_post_prompt"):
            func = getattr(helper, "build_img_thread_followup_post_prompt")
            try:
                prompt = func(
                    thread,
                    target_reply,
                    quoted_own_posts=list(quoted_own_posts or []),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=channel_summary,
                    early_context_lines=early_lines,
                    macro_topic_summary=macro_topic_summary,
                    target_context_lines=target_context_lines,
                )
            except TypeError:
                try:
                    prompt = func(
                        thread,
                        target_reply,
                        quoted_own_posts=list(quoted_own_posts or []),
                        bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                        max_chars=max_chars,
                        character_guidance=character_guidance,
                        social_guidance="",
                        memory_lines=memory_lines,
                        habit_profile_lines=[],
                        channel_summary=channel_summary,
                        early_context_lines=early_lines,
                        macro_topic_summary=macro_topic_summary,
                    )
                except TypeError:
                    fallback_parts = list(early_lines)
                    if target_context_lines:
                        fallback_parts.append("【対象レスの周辺文脈（直前の会話・他人のツッコミ）】\n" + "\n".join(target_context_lines))
                    if macro_topic_summary:
                        fallback_parts.append(macro_topic_summary)
                    if channel_summary:
                        fallback_parts.append(channel_summary)
                    fallback_summary = "\n".join(fallback_parts).strip()
                    prompt = func(
                        thread,
                        target_reply,
                        quoted_own_posts=list(quoted_own_posts or []),
                        bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                        max_chars=max_chars,
                        character_guidance=character_guidance,
                        social_guidance="",
                        memory_lines=memory_lines,
                        habit_profile_lines=[],
                        channel_summary=fallback_summary,
                    )
            if not quoted_own_posts and prompt:
                prompt = prompt.replace(
                    "あなたが前に書いたレスに対して、新しく本文引用つきのレスが来ました。",
                    "スレッド内で盛り上がっている以下の注目レスに対して、引用（>）をつけて会話に参加します。",
                )
                prompt = prompt.replace(
                    "あなたが前に書いたレス:\n(今回引用された自分のレスは特定できていない)\n\n",
                    "",
                )
                prompt = prompt.replace(
                    "新しく来た返信レス:\n",
                    "返信対象の注目レス:\n",
                )
        elif helper is not None and hasattr(helper, "build_img_thread_post_prompt"):
            func = getattr(helper, "build_img_thread_post_prompt")
            try:
                prompt = func(
                    thread,
                    requester_text=requester_text or (content(message) if message is not None else ""),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=channel_summary,
                    early_context_lines=early_lines,
                    macro_topic_summary=macro_topic_summary,
                )
            except TypeError:
                fallback_parts = list(early_lines)
                if macro_topic_summary:
                    fallback_parts.append(macro_topic_summary)
                if channel_summary:
                    fallback_parts.append(channel_summary)
                fallback_summary = "\n".join(fallback_parts).strip()
                prompt = func(
                    thread,
                    requester_text=requester_text or (content(message) if message is not None else ""),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=fallback_summary,
                )

        # プロンプト内の指示テキスト強化（周辺レス内在化・他人事メタ実況厳禁、キメラ疑問文禁止＆当事者アドバイス化、スレ外固有名詞捏造厳禁、前提借用・用語崩れ・安心断定厳禁、思考ステップの対象特定・立場確認）
        meta_commentary_guidance = (
            "【周辺レス・ツッコミの内在化義務と他人事メタ実況の厳禁】\n"
            "- 『〇〇ってツッコミが来てますけど』のように他人の発言をメタ実況することや、既出ツッコミの語尾変えパクり・キメラ合体は絶対厳禁です。\n"
            "- ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。\n\n"
            "【キメラ疑問文の禁止＆相談への当事者アドバイス化】\n"
            "- 他人の発言を適当に繋ぎ合わせたキメラ疑問文は禁止です。既出と被らない独自の視点でズバッと返してください。\n\n"
            "【スレッド外の固有名詞・人名の捏造厳禁】\n"
            "- スレッド内の本文やレスに登場しない人物名・選手名（「〜の二番煎じ」等）を勝手に捏造・引用することは厳禁です。スレッドに出てきた名前だけを使うか、名前を出さずに描写・内容だけで反応してください。\n\n"
            "【前提借用・用語崩れ・安心断定の厳禁】\n"
            "- 相手が言っていない経歴・期間（例:「4年やってる」）を前提にしないこと。引用したレスに書かれていることだけを前提にすること。\n"
            "- 用語はスレッドで使われている通りの形で書くこと（「クリーンインストール」を「クリーンイン」のように途中で切らないこと）。ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。\n"
            "- 安全性が未確定の話題では「安心ですよ」と無条件に断定せず、条件付き（「〜なら」「〜を確認して」）で書くか、分からないことは分からないと書くこと。\n\n"
            "【思考ステップでの対象特定と立場確認（Step A / Step B）】\n"
            "- Step A（文脈把握）: 対象レス本人の主張を1つ特定すること。引用チェーンの中にある他人の発言は、対象本人の主張と混同しないこと。あわせて、周辺文脈のツッコミから、スレの支配的な立場が対象の主張に賛成か反対かを確認すること。\n"
            "- Step B（カウンター方針）: 反論・ツッコミは対象レス本人の主張に向けること。引用チェーン内だけに現れる語句（表示される引用にも残らないもの）を、対象が言ったこととして扱わないこと。スレの支配的な立場が既に退けた主張を蒸し返す場合は、周辺文脈にない新しい切り口を思考メモに明記できた場合に限ること。"
        )

        if slang_guidance_lines:
            slang_note = "【スレッド用語・スラング注意メモ】\n" + "\n".join(slang_guidance_lines)
            if slang_note not in prompt:
                prompt = f"{prompt}\n\n{slang_note}".strip()

        if meta_commentary_guidance not in prompt:
            prompt = f"{prompt}\n\n{meta_commentary_guidance}".strip()

        if memory_store is not None and cfg_bool("DISCUSSED_TOPICS_ENABLED", True) and globals().get("discussed_topics") is not None:
            try:
                _dt_topics = await discussed_topics.fetch_recent_discussed_topics(memory_store, persona=_cfg_persona_namespace(custom_cfg=cfg))
                _dt_block = str(discussed_topics.build_discussed_topics_block(_dt_topics, current_text=" ".join(filter(None, [str(thread.get("title") or ""), str(thread.get("op_text") or ""), str((target_reply or {}).get("text") or ""), *(str(p.get("text") or "") for p in posts[-5:] if isinstance(p, dict))]))) or "").strip()
                prompt = f"{prompt}\n\n{_dt_block}".strip() if _dt_block else prompt
            except Exception as _dt_exc:
                log.debug("[hutaba] 既出話題ブロック取得失敗(fail-open): %r", _dt_exc)

        _lessons_mod = globals().get("lessons")
        if memory_store is not None and cfg_bool("HUTABA_LESSONS_INJECT_ENABLED", True) and _lessons_mod is not None:
            try:
                _ls_raw = _lessons_mod.fetch_active_lessons(memory_store, persona=_cfg_persona_namespace(custom_cfg=cfg), scope="hutaba_post")
                _ls_items = (await _ls_raw) if inspect.isawaitable(_ls_raw) else _ls_raw
                _ls_block = str(_lessons_mod.build_lessons_block(_ls_items) or "").strip() if _ls_items else ""
                prompt = f"{prompt}\n\n{_ls_block}".strip() if _ls_block else prompt
            except Exception as _ls_exc:
                log.debug("[hutaba] 教訓ブロック取得失敗(fail-open): %r", _ls_exc)

        # Discord用の長大システムプロンプト（self._reply_system_prompt()）ではなく、
        # ふたば用のスリム化された人格・口調ガイダンスを base_system_prompt として渡すことでプロンプト肥大化を抑制
        base_system_prompt = character_guidance
        system_prompt = (
            getattr(helper, "build_img_thread_post_system_prompt")(base_system_prompt)
            if helper is not None and hasattr(helper, "build_img_thread_post_system_prompt")
            else f"{base_system_prompt}\nあなたはふたば☆ちゃんねる（img）のとしあき達と短文で雑談するAIです。相手呼称（さん付け等）は禁止です。"
        )
        if meta_commentary_guidance not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{meta_commentary_guidance}".strip()


        return _HutabaReplyPromptContext(
            prompt=prompt,
            system_prompt=system_prompt,
            posts=posts,
            thread_terms=thread_terms,
            frequent_terms=frequent_terms,
            persona_names=persona_names,
            attached_image_description=attached_image_description,
            memory_store=memory_store,
        )
