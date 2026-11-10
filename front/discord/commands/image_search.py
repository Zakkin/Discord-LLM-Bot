"""画像検索コマンド（スラッシュコマンド & コンテキストメニュー）を担当するMixin。"""
from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ollama_bot.common.config_helpers import cfg
from front.discord.common.message_media import message_has_image
from lib.media_utils import is_image_attachment, is_image_url
from lib.text_utils import extract_urls

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _ImageSearchBase = OllamaChatProtocol
else:
    _ImageSearchBase = object

log = logging.getLogger("front.discord.commands.image_search")


class OllamaChatImageSearchMixin(_ImageSearchBase):
    """画像検索コマンド Mixin"""

    @app_commands.command(
        name="image_search",
        description="指定した画像や直前の画像の元ネタ・人物・作品を画像検索で調べます",
    )
    @app_commands.describe(
        image="検索したい画像ファイル（添付）",
        url="検索したい画像のWeb URL",
        query="追加の絞り込みキーワードや質問（任意）",
    )
    async def slash_image_search(
        self,
        interaction: discord.Interaction,
        image: Optional[discord.Attachment] = None,
        url: Optional[str] = None,
        query: Optional[str] = None,
    ) -> None:
        """スラッシュコマンド /image_search のハンドラー (BAN-161, BAN-162準拠)"""
        await interaction.response.defer(thinking=True)

        user = getattr(interaction, "user", None)
        log.info(
            "[IMAGE_SEARCH_CMD] Slash /image_search triggered: user=%s (id=%s), channel=%s, has_image=%s, url='%s', query='%s'",
            user, getattr(user, "id", None), getattr(interaction, "channel", None), bool(image), url or "", query or ""
        )

        target_url = ""
        # 1. 添付画像
        if image and is_image_attachment(image):
            target_url = getattr(image, "url", "")
        # 2. 引数URL
        elif url and url.strip():
            target_url = url.strip()
        # 3. チャンネル内の直近メッセージ探索（過去10件）
        else:
            channel = getattr(interaction, "channel", None)
            if isinstance(channel, (discord.TextChannel, discord.Thread)) or (channel is not None and hasattr(channel, "history")):
                try:
                    async for msg in channel.history(limit=10):
                        if msg.attachments:
                            for att in msg.attachments:
                                if is_image_attachment(att):
                                    target_url = getattr(att, "url", "")
                                    break
                        if target_url:
                            break
                        urls = extract_urls(getattr(msg, "content", "") or "")
                        for u in urls:
                            if is_image_url(u):
                                target_url = u
                                break
                        if target_url:
                            break
                except Exception as e:
                    log.warning("[image_search] Failed to search channel history: %r", e)

        if target_url:
            log.info("[IMAGE_SEARCH_CMD] Resolved target image URL: '%s'", target_url)
        else:
            log.info("[IMAGE_SEARCH_CMD] No image found in attachment, url, or channel history")

        if not target_url:
            await interaction.followup.send(
                "ぴゃっ…！検索対象の画像が見つかりませんでした…！\n画像を添付するか、画像URLを指定して呼び出してください…！",
                ephemeral=True,
            )
            return

        await self._execute_and_respond_image_search(interaction, target_url, query=query or "")

    async def context_image_search(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """メッセージ右クリック -> アプリ -> 「画像検索」のハンドラー (BAN-161, BAN-162準拠)"""
        await interaction.response.defer(thinking=True)

        user = getattr(interaction, "user", None)
        log.info(
            "[IMAGE_SEARCH_MENU] Context menu '画像検索' triggered: user=%s (id=%s), target_msg_id=%s, author=%s",
            user, getattr(user, "id", None), getattr(message, "id", None), getattr(message, "author", None)
        )

        target_url = ""
        if getattr(message, "attachments", None):
            for att in message.attachments:
                if is_image_attachment(att):
                    target_url = getattr(att, "url", "")
                    break

        if not target_url:
            urls = extract_urls(getattr(message, "content", "") or "")
            for u in urls:
                if is_image_url(u):
                    target_url = u
                    break

        if target_url:
            log.info("[IMAGE_SEARCH_MENU] Resolved target image URL: '%s' from message_id=%s", target_url, getattr(message, "id", None))
        else:
            log.info("[IMAGE_SEARCH_MENU] Target message_id=%s contains no image", getattr(message, "id", None))

        if not target_url:
            await interaction.followup.send(
                "ぴゃっ…！このメッセージには画像が添付されていないみたいですよ…！",
                ephemeral=True,
            )
            return

        await self._execute_and_respond_image_search(interaction, target_url, query="")

    async def _execute_and_respond_image_search(
        self,
        interaction: discord.Interaction,
        image_url: str,
        query: str = "",
    ) -> None:
        """画像検索を実行し結果をEmbedで送信する共通内部メソッド"""
        log.info("[IMAGE_SEARCH_EXEC] Calling search_image_unified: image_url='%s', query='%s'", image_url, query)
        try:
            from front.discord.common.message_media import (
                describe_image_for_identification,
                verify_image_candidates,
            )
            from harness.agent.tool_registry.adapters.image_search import ImageSearchToolAdapter
            try:
                res = await ImageSearchToolAdapter.search_image_unified(
                    image_url,
                    query=query,
                    vision_describe=describe_image_for_identification,
                    vision_verify=verify_image_candidates,
                )
            except TypeError:
                res = await ImageSearchToolAdapter.search_image_unified(image_url, query=query)
        except ImportError:
            # tool_registry 未完了時のモック・フォールバック
            res = {
                "success": True,
                "source": "fallback",
                "text": f"画像URL（{image_url}）の検索リクエストを受け付けました。",
                "lens_url": f"https://lens.google.com/uploadbyurl?url={image_url}",
            }
        except Exception as e:
            log.exception("[image_search] Execution failed: %r", e)
            res = {"success": False, "text": f"画像検索中にエラーが発生しました: {e}"}

        log.info(
            "[IMAGE_SEARCH_EXEC] Completed: success=%s, source='%s', text_chars=%d, lens_url='%s'",
            res.get("success"), res.get("source"), len(res.get("text", "") or ""), res.get("lens_url", "")
        )

        embed = discord.Embed(
            title="🔍 画像検索結果",
            color=0x4285F4,
        )
        embed.set_thumbnail(url=image_url)
        embed.add_field(name="対象画像", value=f"[画像リンク]({image_url})", inline=False)

        text = str(res.get("text", "") or "")
        if len(text) > 1000:
            text = text[:997] + "..."
        embed.add_field(name="調査結果", value=text or "情報が見つかりませんでした。", inline=False)

        candidates = res.get("candidates") or []
        if candidates:
            cand_lines = []
            for cand in candidates[:3]:
                title = cand.get("title") or "名称不明"
                src_url = cand.get("source_url") or ""
                sim = cand.get("similarity", 0.0)
                verdict = cand.get("verdict", "")
                v_mark = "✅" if verdict == "match" else ("❓" if verdict == "partial" else "")
                cand_lines.append(f"- {v_mark} [{title}](<{src_url}>) (一致度: {sim:.0f}%)" if src_url else f"- {v_mark} {title} (一致度: {sim:.0f}%)")
            if cand_lines:
                embed.add_field(name="候補・出典", value="\n".join(cand_lines), inline=False)

        lens_url = res.get("lens_url")
        if lens_url:
            embed.add_field(name="Google Lens", value=f"[Lensで直接確認]({lens_url})", inline=False)

        footer_name = ""
        try:
            from lib.config_utils import cfg
            footer_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or cfg("BOT_FIRST_PERSON", "") or "").strip()
        except Exception:
            pass

        footer_text = f"{footer_name}画像調査システム" if footer_name else "画像調査システム"
        embed.set_footer(text=footer_text)
        await interaction.followup.send(embed=embed)


__all__ = ["OllamaChatImageSearchMixin"]
