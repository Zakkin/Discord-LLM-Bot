"""Botの稼働状態（起動・停止・ステータス確認）および管理者権限制限を担当するMixin。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, TYPE_CHECKING, cast

import discord
from discord import app_commands

from ollama_bot.common.config_helpers import cfg, cfg_int_set, is_user_command_blocked
from front.discord.common.discord_helpers import author_id, content
from ollama_bot.common.emotion_helpers import emotion_scoring_enabled

try:
    from harness.agent.memory_manager.logic import (
        DEFAULT_BBS_SLANG_DICT,
        SlangEntry,
        load_slangs_from_db,
        upsert_slang,
    )
except (ImportError, AttributeError):
    from typing_extensions import TypedDict

    class SlangEntry(TypedDict, total=False):  # type: ignore[no-redef]
        term: str
        category: str
        meaning: str
        strip_from_query: bool
        is_proper_noun: bool
        guidance: str

    DEFAULT_BBS_SLANG_DICT: dict[str, SlangEntry] = {}  # type: ignore[no-redef]

    async def load_slangs_from_db(store: Any) -> dict[str, SlangEntry]:
        return dict(DEFAULT_BBS_SLANG_DICT)

    async def upsert_slang(store: Any, entry: SlangEntry, source: str = "manual") -> bool:
        DEFAULT_BBS_SLANG_DICT[entry["term"]] = entry
        return True

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _AdminBase = OllamaChatProtocol
else:
    _AdminBase = object

log = logging.getLogger("ollama_bot.ollama_chat_.admin")


def is_bot_admin_user(user_id: int | None) -> bool:
    """指定されたユーザーIDが管理者であるかを判定する。
    
    BOT_ADMIN_USER_IDS または ADMIN_USER_IDS に含まれているかを検証する。
    未設定（空）の場合は安全のため常に False を返す。
    """
    if user_id is None:
        return False
    admin_ids = cfg_int_set("BOT_ADMIN_USER_IDS") or cfg_int_set("ADMIN_USER_IDS")
    return user_id in admin_ids


class OllamaChatAdminMixin(_AdminBase):
    def is_bot_admin_user(self, user_id: int | None) -> bool:
        """指定されたユーザーIDが管理者であるかを判定する。"""
        return is_bot_admin_user(user_id)

    async def set_bot_active(self, active: bool, operator_name: str = "") -> tuple[bool, str]:
        """Botの稼働状態を切り替え、Presenceや実行中タスクを更新する。"""
        current = getattr(self, "_bot_active", True)
        if current == active:
            if active:
                return False, "ℹ️ Botはすでに起動・稼働中です。"
            return False, "ℹ️ Botはすでに停止（スタンバイ）中です。"

        self._bot_active = active

        if not active:
            log.warning("Bot deactivated (stopped) by operator=%s", operator_name)
            # 停止中のPresence設定
            stopped_text = str(cfg("BOT_STOPPED_ACTIVITY_TEXT", "停止中（スタンバイ）") or "停止中（スタンバイ）")
            try:
                status_dnd = getattr(discord.Status, "dnd", "dnd")
                activity = discord.CustomActivity(name=stopped_text)
                await self.bot.change_presence(status=status_dnd, activity=activity)
                if hasattr(self, "emotion_state") and hasattr(self, "_emotion_lock"):
                    async with self._emotion_lock:
                        self.emotion_state.last_applied_activity = stopped_text
            except Exception as e:
                log.warning("Failed to update presence on bot stop: %r", e)

            # 実行中の背景LLMタスクがあればキャンセル
            bg_task = getattr(self, "_current_background_llm_task", None)
            if bg_task is not None and not bg_task.done():
                bg_task.cancel()

            # 歌唱タスクがあれば中断
            if hasattr(self, "singing_tasks"):
                for task in list(self.singing_tasks.values()):
                    if not task.done():
                        task.cancel()
                self.singing_tasks.clear()

            msg = "🛑 **Botの応答機能を停止しました。**\nAIチャット返信および自発発言をスタンバイ状態に移行しました。"
            return True, msg

        log.info("Bot activated (started) by operator=%s", operator_name)
        # 起動時のPresence復帰（感情連動Presenceを強制即時反映）
        try:
            if hasattr(self, "_sync_emotion_presence") and emotion_scoring_enabled():
                await self._sync_emotion_presence(force=True)
            else:
                status_online = getattr(discord.Status, "online", "online")
                await self.bot.change_presence(status=status_online, activity=None)
        except Exception as e:
            log.warning("Failed to restore presence on bot start: %r", e)

        msg = "▶️ **Botの応答機能を起動・再開しました。**\nメッセージ応答および自発発言を再開します。"
        return True, msg

    def set_reply_count_to_max(self, channel_id: int | None = None) -> tuple[list[int], int]:
        """サブチャンネル等の未返信メッセージカウントを上限値に引き上げ、次回レスへの確定返信をセットする。"""
        from ollama_bot.ollama_chat_.ollama_chat_helpers import _get_other_channel_reply_upper_limit, _set_other_channel_message_count_to_max

        other_cids = cfg_int_set("OTHER_CHANNEL_IDS")
        if channel_id is not None and channel_id > 0 and (channel_id in other_cids or not other_cids):
            target_cids = [channel_id]
        else:
            cids_set = set(other_cids)
            if channel_id is not None and channel_id > 0:
                cids_set.add(channel_id)
            target_cids = sorted(list(cids_set))

        upper_limit = _get_other_channel_reply_upper_limit()
        for cid in target_cids:
            _set_other_channel_message_count_to_max(self, cid)

        log.info(
            "Set reply count to max=%d for channels=%s",
            upper_limit,
            target_cids,
        )
        return target_cids, upper_limit

    @app_commands.command(
        name="bot",
        description="Botの稼働状態（起動・停止・確認）を管理します。",
    )
    @app_commands.describe(action="実行する操作（start: 起動, stop: 停止, status: 状態確認, next_reply: 次回即時返信）")
    @app_commands.choices(action=[
        app_commands.Choice(name="start (起動・再開)", value="start"),
        app_commands.Choice(name="stop (一時停止)", value="stop"),
        app_commands.Choice(name="status (状態確認)", value="status"),
        app_commands.Choice(name="次回即時返信（カウント上限）", value="next_reply"),
    ])
    async def slash_bot(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ) -> None:
        cmd_action = getattr(action, "value", str(action))
        user_id = getattr(interaction.user, "id", None)
        operator_name = getattr(interaction.user, "display_name", str(interaction.user))

        if cmd_action == "start":
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /bot start attempt from user_id=%s", user_id)
                await interaction.response.send_message(
                    "❌ このコマンドを実行する権限がありません。（管理者専用）",
                    ephemeral=True,
                )
                return
            _, msg = await self.set_bot_active(True, operator_name=operator_name)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if cmd_action == "stop":
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /bot stop attempt from user_id=%s", user_id)
                await interaction.response.send_message(
                    "❌ このコマンドを実行する権限がありません。（管理者専用）",
                    ephemeral=True,
                )
                return
            _, msg = await self.set_bot_active(False, operator_name=operator_name)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if cmd_action in ("next_reply", "force_reply", "max"):
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /bot %s attempt from user_id=%s", cmd_action, user_id)
                await interaction.response.send_message(
                    "⛔ このコマンドを実行する権限がありません（管理者専用）。",
                    ephemeral=True,
                )
                return
            channel_id = getattr(interaction, "channel_id", None)
            if channel_id is None:
                channel = getattr(interaction, "channel", None)
                channel_id = getattr(channel, "id", None)
            _, upper_limit = self.set_reply_count_to_max(channel_id)
            await interaction.response.send_message(
                f"⚡ 返信カウントを上限（`{upper_limit}`）に設定しました。次のレスに即座に返信し、その後は通常の設定インターバルに戻ります。",
                ephemeral=True,
            )
            return

        # status
        active = getattr(self, "is_active", True)
        if active:
            msg = "🟢 **Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
        else:
            msg = "🔴 **Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_start",
        description="【管理者専用】Botの応答機能を起動・再開します",
    )
    async def slash_bot_start(self, interaction: discord.Interaction) -> None:
        user_id = getattr(interaction.user, "id", None)
        if not self.is_bot_admin_user(user_id):
            log.warning("Unauthorized /bot_start attempt from user_id=%s", user_id)
            await interaction.response.send_message(
                "❌ このコマンドを実行する権限がありません。（管理者専用）",
                ephemeral=True,
            )
            return

        operator_name = getattr(interaction.user, "display_name", str(interaction.user))
        _, msg = await self.set_bot_active(True, operator_name=operator_name)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_stop",
        description="【管理者専用】Botの応答機能を一時停止します",
    )
    async def slash_bot_stop(self, interaction: discord.Interaction) -> None:
        user_id = getattr(interaction.user, "id", None)
        if not self.is_bot_admin_user(user_id):
            log.warning("Unauthorized /bot_stop attempt from user_id=%s", user_id)
            await interaction.response.send_message(
                "❌ このコマンドを実行する権限がありません。（管理者専用）",
                ephemeral=True,
            )
            return

        operator_name = getattr(interaction.user, "display_name", str(interaction.user))
        _, msg = await self.set_bot_active(False, operator_name=operator_name)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_status",
        description="Botの現在の稼働状態（稼働中／停止中）を確認します",
    )
    async def slash_bot_status(self, interaction: discord.Interaction) -> None:
        active = getattr(self, "is_active", True)
        if active:
            msg = "🟢 **Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
        else:
            msg = "🔴 **Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_next_reply",
        description="【管理者専用】返信カウントを上限にし、次のレスに即座に返信させます",
    )
    async def slash_bot_next_reply(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            log.warning("Interaction timed out or already responded: %r", e)
            return
        except TypeError:
            # モック環境等で defer が await 不可の MagicMock の場合の安全策
            pass

        async def _send_reply(message: str) -> None:
            try:
                if hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
                    try:
                        await interaction.followup.send(message, ephemeral=True)
                        return
                    except TypeError:
                        # モック環境等で followup.send が await 不可の MagicMock の場合のフォールバック
                        pass
                await interaction.response.send_message(message, ephemeral=True)
            except (discord.NotFound, discord.HTTPException) as e:
                log.warning("Interaction timed out or already responded: %r", e)

        user_id = getattr(interaction.user, "id", None)
        if not self.is_bot_admin_user(user_id):
            log.warning("Unauthorized /bot_next_reply attempt from user_id=%s", user_id)
            await _send_reply("⛔ このコマンドを実行する権限がありません（管理者専用）。")
            return

        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None:
            channel = getattr(interaction, "channel", None)
            channel_id = getattr(channel, "id", None)
        _, upper_limit = self.set_reply_count_to_max(channel_id)
        await _send_reply(
            f"⚡ 返信カウントを上限（`{upper_limit}`）に設定しました。次のレスに即座に返信し、その後は通常の設定インターバルに戻ります。"
        )

    @app_commands.command(
        name="slang",
        description="掲示板スラング辞書の調査・自動登録・登録内容の確認を行います。",
    )
    @app_commands.describe(
        action="実行する操作（learn: 調査してDB登録, show: 登録内容の表示, list: 登録一覧）",
        term="対象のスラング・単語（learn または show 時に指定）",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="learn (Web調査してDB登録)", value="learn"),
        app_commands.Choice(name="show (登録内容の詳細確認)", value="show"),
        app_commands.Choice(name="list (登録スラング一覧)", value="list"),
    ])
    async def slash_slang(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        term: str = "",
    ) -> None:
        cmd_action = getattr(action, "value", str(action))
        user_id = getattr(interaction.user, "id", None)
        operator_name = getattr(interaction.user, "display_name", str(interaction.user))
        term_clean = term.strip()

        # 1. 一覧表示 (list)
        if cmd_action == "list":
            await interaction.response.defer(ephemeral=True)
            store = getattr(self, "memory_store", None)
            slangs = await load_slangs_from_db(store)
            total = len(slangs)
            terms_preview = "、".join(list(slangs.keys())[:25])
            if total > 25:
                terms_preview += f" ...他（計 {total} 件）"

            embed = discord.Embed(
                title="📚 掲示板スラング辞書 登録一覧",
                description=f"**総登録件数:** {total} 件\n\n**登録単語:**\n{terms_preview or '登録なし'}",
                color=0x3498DB,
            )
            embed.set_footer(text="各単語の詳細は /slang action:show term:<単語> で確認できます")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 2. 詳細確認 (show)
        if cmd_action == "show":
            if not term_clean:
                await interaction.response.send_message("❌ 確認したいスラングの単語を `term` に指定してください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            store = getattr(self, "memory_store", None)
            slangs = await load_slangs_from_db(store)
            entry = slangs.get(term_clean)
            if not entry:
                await interaction.followup.send(f"ℹ️ スラング 『{term_clean}』 はまだ登録されていません。\n`/slang action:learn term:{term_clean}` で調査・登録できます。", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📖 スラング詳細: 『{entry.get('term')}』",
                color=0x2ECC71,
            )
            embed.add_field(name="カテゴリ", value=str(entry.get("category", "slang")), inline=True)
            embed.add_field(name="検索クエリ除外", value="✅ 除外する (ON)" if entry.get("strip_from_query") else "❌ 除外しない (OFF)", inline=True)
            embed.add_field(name="固有名詞判定", value="あり（キャラ名等）" if entry.get("is_proper_noun") else "なし（前置き/一般俗語）", inline=True)
            embed.add_field(name="意味・説明", value=str(entry.get("meaning", "なし")), inline=False)
            embed.add_field(name="Bot向けガイダンス（プロンプト注記）", value=str(entry.get("guidance", "なし")), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 3. 調査してDB登録 (learn)
        if cmd_action == "learn":
            if not term_clean:
                await interaction.response.send_message("❌ 調査・登録したいスラングの単語を `term` に指定してください。", ephemeral=True)
                return

            # 管理者のみ実行可能
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /slang learn attempt for term=%r from user_id=%s", term_clean, user_id)
                await interaction.response.send_message("❌ このコマンドを実行する権限がありません。（管理者専用）", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=False)
            log.info("[slang_admin] Starting slang research via Discord: term=%r operator=%s", term_clean, operator_name)

            entry = await self._research_and_classify_slang(term_clean)
            if not entry:
                await interaction.followup.send(f"⚠️ スラング 『{term_clean}』 の調査・意味解析に失敗しました。")
                return

            store = getattr(self, "memory_store", None)
            success = await upsert_slang(store, entry, source=f"discord:{operator_name}")
            if not success:
                await interaction.followup.send(f"❌ スラング 『{term_clean}』 のDB登録に失敗しました。")
                return

            log.info(
                "[slang_admin] Successfully learned slang: term=%r category=%r strip_from_query=%s guidance=%r operator=%s",
                entry.get("term"),
                entry.get("category"),
                entry.get("strip_from_query"),
                entry.get("guidance"),
                operator_name,
            )

            # 登録結果をEmbedで分かりやすく報告
            embed = discord.Embed(
                title=f"✅ スラングを調査・DB登録しました: 『{entry.get('term')}』",
                color=0x9B59B6,
            )
            embed.add_field(name="カテゴリ", value=str(entry.get("category")), inline=True)
            embed.add_field(name="検索クエリ除外", value="✅ 除外する (ON)" if entry.get("strip_from_query") else "❌ 除外しない (OFF)", inline=True)
            embed.add_field(name="固有名詞判定", value="あり" if entry.get("is_proper_noun") else "なし", inline=True)
            embed.add_field(name="意味", value=str(entry.get("meaning")), inline=False)
            embed.add_field(name="Botプロンプト注記", value=str(entry.get("guidance")), inline=False)
            embed.set_footer(text=f"登録者: {operator_name} | source: discord")
            await interaction.followup.send(embed=embed)
            return

    async def _research_and_classify_slang(self, term: str) -> SlangEntry | None:
        """指定された単語をWeb検索し、LLMでスラング属性（分類、意味、検索除外設定、注記）を構造化抽出する。"""
        # 1. Web調査ディスパッチ
        search_query = f"{term} スラング 意味 ネット用語 掲示板"
        summary_text = ""
        try:
            from front.discord.reply.reply_core import research_dispatch
            res = await asyncio.wait_for(
                research_dispatch(search_query, mode="simple_search", max_results=3),
                timeout=15.0,
            )
            if isinstance(res, dict):
                summary_text = str(res.get("summary") or res.get("text") or "").strip()
            elif isinstance(res, str):
                summary_text = res.strip()
        except Exception as e:
            log.warning("[slang_admin] Web research failed for term=%r: %r", term, e)

        # 2. LLMによる構造化解析
        try:
            from ollama_bot.common.ollama_helpers.structured import call_ollama_json
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["prefix_slang", "reaction_meme", "community_term", "archetype", "slang"]
                    },
                    "meaning": {"type": "string"},
                    "strip_from_query": {"type": "boolean"},
                    "is_proper_noun": {"type": "boolean"},
                    "guidance": {"type": "string"}
                },
                "required": ["term", "category", "meaning", "strip_from_query", "is_proper_noun", "guidance"],
                "additionalProperties": False,
            }

            prompt = (
                f"あなたはネット掲示板・コミュニティスラングの分類・分析AIです。\n"
                f"対象単語: 『{term}』\n"
                f"Web検索結果:\n{summary_text or '（検索結果なし。言語知識から分析してください）'}\n\n"
                f"【判定基準】\n"
                f"- category:\n"
                f"  - prefix_slang: 質問や話題の前置き略語（例: すみフラ、うろフラ、チラ裏等）。\n"
                f"  - reaction_meme: 定型ツッコミ・相槌（例: なそ、にん、そうだね等）。\n"
                f"  - community_term: 住人の呼び名やシステム用語（例: 「」、としあき、定時等）。\n"
                f"  - archetype: キャラ属性（例: ロリババア、メスガキ等）。\n"
                f"  - slang: 一般的なスラング・俗語。\n"
                f"- strip_from_query: 質問や話題の本体ではなく、検索クエリに含めるとノイズになる前置き（prefix_slang）やリアクション定型句の場合は true。\n"
                f"- is_proper_noun: 作品名や特定キャラクター名である場合は true、単なる前置きや俗語なら false。\n"
                f"- guidance: AIがこの単語に遭遇した際にキャラ名と誤認しないための簡潔な注意書き。\n"
            )

            try:
                parsed = await asyncio.wait_for(
                    call_ollama_json(
                        prompt,
                        system_prompt="スラング分析AI。指定JSONフォーマットのみを返してください。",
                        schema=schema,
                        model="middle-llm",
                        temperature=0.2,
                    ),
                    timeout=15.0,
                )
            except TypeError:
                messages = [
                    {"role": "system", "content": "スラング分析AI。指定JSONフォーマットのみを返してください。"},
                    {"role": "user", "content": prompt},
                ]
                parsed = await asyncio.wait_for(
                    call_ollama_json(
                        model="middle-llm",
                        messages=messages,
                        format=schema,
                        temperature=0.2,
                    ),
                    timeout=15.0,
                )
            if isinstance(parsed, dict) and parsed.get("term"):
                parsed["term"] = term  # 表記を保証
                return cast(SlangEntry, parsed)
        except Exception as e:
            log.warning("[slang_admin] LLM slang extraction failed for term=%r: %r", term, e)

        # フォールバック（LLM失敗時）
        fallback_entry: SlangEntry = {
            "term": term,
            "category": "slang",
            "meaning": summary_text[:120] if summary_text else "ネットスラング・俗語",
            "strip_from_query": False,
            "is_proper_noun": False,
            "guidance": f"『{term}』に関する話題です。キャラクター名と決めつけず文脈を尊重してください。",
        }
        return fallback_entry

    def _get_bot_identifiers(self) -> tuple[set[str], set[str]]:
        """現在のBotの識別名一覧と、他Botの既知の識別名一覧を返す。"""
        from front.discord.common.bot_identity import get_bot_identifiers
        return get_bot_identifiers(getattr(self, "bot", None))

    def _is_target_of_admin_command(self, message: discord.Message, parts: list[str]) -> bool:
        """このBotインスタンスが !bot テキストコマンドの処理対象であるかを判定する。
        
        複数Botが同居するサーバーにおいて、コマンドが全Botで重複実行されるのを防ぐ。
        """
        from front.discord.common.bot_identity import is_target_of_command
        return is_target_of_command(getattr(self, "bot", None), message, tokens=parts, is_admin_command=True)

    async def _handle_admin_text_command(self, message: discord.Message) -> bool:
        """`!bot` 形式のテキストコマンドを処理する。
        
        戻り値: コマンドとして処理された場合は True、対象外の場合は False。
        """
        raw_text = content(message).strip()
        parts = raw_text.split()
        if not parts or parts[0].lower() != "!bot":
            return False

        # 複数Bot同居時の宛先チェック（他Bot宛て、または代表Botでない場合はスルー）
        if not self._is_target_of_admin_command(message, parts):
            return False

        u_id = author_id(message)
        if is_user_command_blocked(u_id):
            log.info("TRACE _handle_admin_text_command: blocked command ignored user %s", u_id)
            return True

        from ollama_bot.config_loader import config
        bot_disp_name = getattr(config, "OLLAMA_BOT_DISPLAY_NAME", "") or "Bot"
        prefix_tag = f"【{bot_disp_name}】"

        # サブコマンドの判定 (parts[1] が target 名の場合も考慮)
        valid_subcommands = {"start", "stop", "status", "next", "next_reply", "force_reply", "max", "reply"}
        found_subcommand = None
        for p in parts[1:]:
            p_lower = p.lower()
            if p_lower in valid_subcommands:
                found_subcommand = p_lower
                break

        self_identifiers, other_configs = self._get_bot_identifiers()
        known_targets = self_identifiers | other_configs | {"all", "全員", "全部"}

        if found_subcommand:
            subcommand = found_subcommand
        elif len(parts) == 1:
            subcommand = "status"
        else:
            args_lower = [p.lower() for p in parts[1:]]
            if all(arg in known_targets for arg in args_lower):
                subcommand = "status"
            else:
                subcommand = parts[1].lower()

        author_name = getattr(message.author, "display_name", str(message.author))

        if subcommand == "start":
            if not self.is_bot_admin_user(u_id):
                log.warning("Unauthorized !bot start attempt from author_id=%s", u_id)
                await message.reply(f"❌ {prefix_tag} このコマンドを実行する権限がありません。（管理者専用）", mention_author=False)
                return True
            _, msg = await self.set_bot_active(True, operator_name=author_name)
            await message.reply(f"{prefix_tag} {msg}", mention_author=False)
            return True

        if subcommand == "stop":
            if not self.is_bot_admin_user(u_id):
                log.warning("Unauthorized !bot stop attempt from author_id=%s", u_id)
                await message.reply(f"❌ {prefix_tag} このコマンドを実行する権限がありません。（管理者専用）", mention_author=False)
                return True
            _, msg = await self.set_bot_active(False, operator_name=author_name)
            await message.reply(f"{prefix_tag} {msg}", mention_author=False)
            return True

        if subcommand in {"next", "next_reply", "force_reply", "max", "reply"}:
            if not self.is_bot_admin_user(u_id):
                log.warning("Unauthorized !bot %s attempt from author_id=%s", subcommand, u_id)
                await message.reply(f"❌ {prefix_tag} このコマンドを実行する権限がありません。（管理者専用）", mention_author=False)
                return True
            cid = getattr(getattr(message, "channel", None), "id", 0) or 0
            _, upper_limit = self.set_reply_count_to_max(cid)
            msg = f"⚡ {prefix_tag} 返信カウントを上限（`{upper_limit}`）に設定しました。次のレスに即座に返信し、その後は通常の設定インターバルに戻ります。"
            await message.reply(msg, mention_author=False)
            return True

        if subcommand == "status":
            active = getattr(self, "is_active", True)
            if active:
                msg = f"🟢 **{prefix_tag} Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
            else:
                msg = f"🔴 **{prefix_tag} Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
            await message.reply(msg, mention_author=False)
            return True

        # 未知のサブコマンド
        await message.reply(
            f"ℹ️ {prefix_tag} 使用法: `!bot start`（起動・再開）, `!bot stop`（停止）, `!bot status`（状態確認）, `!bot next`（次回即時返信）\n"
            f"*（💡 個別指定: `!bot <cmd> <bot名>` / 一括指定: `!bot <cmd> all`）*",
            mention_author=False,
        )
        return True
