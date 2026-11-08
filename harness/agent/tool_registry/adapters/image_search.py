"""AIエージェント・ハーネス 画像検索ツールアダプター (adapters/image_search.py)

画像URLから Google Lens (Chrome MCP) / Web検索 を用いて
写っている人物・キャラクター名・作品名・元ネタ・出典を特定する ToolDefinition を提供する。
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter, ToolResult
from ..web_research.client import (
    _browser_operation_guard,
    _build_navigate_args,
    _close_browser_tab_if_possible,
    _get_shared_mcp_client,
    _read_browser_content_with_retries,
    _sleep_after_browser_navigation,
)
from ..web_research.parsing import (
    _extract_tab_id,
    _extract_text_from_tool_result,
    _should_close_browser_tab_after_navigation,
)

log = logging.getLogger("harness.agent.tool_registry.adapters.image_search")

_IMAGE_URL_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:\?.*)?$", re.IGNORECASE)
_DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>\"']+",
    re.IGNORECASE,
)

_LENS_ERROR_MARKERS = (
    "画像を読み込めませんでした",
    "画像を検索できませんでした",
    "urlから画像を取得できません",
    "画像が見つかりません",
    "couldn't search this image",
    "image could not be loaded",
    "can't find any results",
    "一致する結果はありませんでした",
    "視覚的な一致は見つかりませんでした",
    "no matching images found",
    "ログインして続行",
    "アカウントを作成",
    "cookie の使用に同意",
)


def _is_lens_error_or_wall(text: str) -> bool:
    """Google Lens の取得結果がエラー画面またはログイン壁であるかを判定する"""
    lowered = text.lower()
    return any(marker in lowered for marker in _LENS_ERROR_MARKERS)


_QUESTION_PHRASES_RE = re.compile(
    r"(?:画像検索|これ誰(?:ですか|だ|？|\?)?|この(?:キャラ|人|画像|写真|女の子|男の子)誰(?:ですか|だ|？|\?)?|"
    r"誰(?:ですか|だ|？|\?)?|何の(?:キャラ|キャラクター|作品|アニメ|ゲーム|画像|元ネタ)(?:ですか|だ|？|\?)?|"
    r"元ネタ(?:は何|は誰|教えて|は|ですか|だ|？|\?)?|これ何(?:ですか|だ|？|\?)?|何(?:ですか|だ|？|\?)?|"
    r"教えて(?:ください|よ)?|調べ(?:て|てください)?)",
    re.IGNORECASE,
)


def _clean_fallback_query(query: str, image_url: str) -> str:
    """フォールバックWeb検索用のクエリをクレンジングする。

    - 画像URLそのものは無効化
    - 質問定型句を除去し、意味のある単語が残ればそれを採用
    - キーワードがない場合、画像URLのパス末尾からファイル名（ハッシュや数字のみを除外）を抽出
    """
    clean = str(query or "").strip()
    if clean.startswith("http://") or clean.startswith("https://"):
        clean = ""

    if clean:
        stripped = _QUESTION_PHRASES_RE.sub(" ", clean)
        stripped = " ".join(stripped.split()).strip()
        if stripped:
            return stripped

    # キーワードがない場合、画像URLからファイル名抽出を試みる
    if image_url:
        try:
            parsed = urllib.parse.urlparse(image_url)
            path = parsed.path.rstrip("/")
            if path:
                raw_filename = path.split("/")[-1]
                filename = re.sub(r"\.(?:png|jpe?g|webp|gif|bmp)(?:\?.*)?$", "", raw_filename, flags=re.IGNORECASE)
                filename = urllib.parse.unquote(filename).strip()
                if filename and not re.fullmatch(r"[0-9_\-]+", filename):
                    if not re.fullmatch(r"[0-9a-fA-F_\-]{8,}", filename):
                        if not re.fullmatch(
                            r"(?:img|image|photo|picture|screenshot|スクリーンショット)[0-9_\-]*",
                            filename,
                            re.IGNORECASE,
                        ):
                            if len(filename) >= 2:
                                return filename
        except Exception:
            pass

    return ""


def extract_image_url_from_context(context: dict[str, Any] | Any = None, **kwargs: Any) -> str:
    """コンテキストや kwargs から最も関連性の高い画像URLを安全に抽出する。"""
    candidates: list[str] = []

    # kwargs からの直接取得
    for key in (
        "image_url",
        "target_image_url",
        "recent_image_url",
        "last_image_url",
        "attachment_url",
        "url",
    ):
        val = kwargs.get(key)
        if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
            candidates.append(val.strip())

    # context 辞書からの取得
    if isinstance(context, dict):
        for key in (
            "image_url",
            "target_image_url",
            "recent_image_url",
            "last_image_url",
            "attachment_url",
            "url",
        ):
            val = context.get(key)
            if val and isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
                candidates.append(val.strip())

        # attachments / images リスト
        atts = context.get("attachments") or context.get("images")
        if isinstance(atts, (list, tuple)):
            for item in atts:
                if isinstance(item, str) and (item.startswith("http://") or item.startswith("https://")):
                    candidates.append(item.strip())
                elif isinstance(item, dict) and item.get("url"):
                    candidates.append(str(item["url"]).strip())
                elif hasattr(item, "url") and getattr(item, "url"):
                    candidates.append(str(getattr(item, "url")).strip())

        # テキスト内の Discord CDN / 画像URL探索
        for text_key in ("user_text", "prompt", "context_text", "last_message", "message"):
            txt = str(context.get(text_key) or "")
            if not txt:
                continue
            found_discord = _DISCORD_CDN_RE.findall(txt)
            candidates.extend(found_discord)
            for u in re.findall(r"https?://[^\s<>\"']+", txt):
                if _IMAGE_URL_EXT_RE.search(u):
                    candidates.append(u.strip())

    # 画像特有URL（Discord CDNまたは画像拡張子）を最優先
    for cand in candidates:
        if _DISCORD_CDN_RE.search(cand) or _IMAGE_URL_EXT_RE.search(cand):
            return cand

    # それ以外の http/https URL
    for cand in candidates:
        if cand.startswith("http://") or cand.startswith("https://"):
            return cand

    return ""


class ImageSearchToolAdapter:
    """画像検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_image", timeout_sec: float = 40.0) -> ToolDefinition:
        """search_image ツール定義を作成する（デフォルトタイムアウト: 40秒）"""
        return ToolDefinition(
            name=name,
            description=(
                "画像URLを指定して逆画像検索を行い、写っている人物・キャラクター名・作品名・元ネタ・類似画像・出典Webページを客観的に調査します。"
                "添付画像や直前の画像について『これ誰？』『何のキャラ？』『元ネタは何？』と質問された場合に使用してください。"
            ),
            parameters=[
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="検索対象の画像Web URL（http/https）。省略時は直近の添付画像や文脈上の画像が自動適用されます。",
                    required=False,
                ),
                ToolParameter(
                    name="query",
                    type="string",
                    description="追加の絞り込みキーワードや質問（キャラクター名、推測される特徴など。任意）",
                    required=False,
                ),
            ],
            handler=cls.execute_image_search,
            category=ToolCategory.WEB,
            tags={"web", "search", "image", "research"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    async def execute_image_search(
        cls,
        image_url: str = "",
        query: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """画像検索ツールの実行エントリーポイント"""
        resolved_url = (image_url or "").strip()
        if not resolved_url:
            resolved_url = extract_image_url_from_context(kwargs.get("context"), **kwargs)

        clean_query = str(query or "").strip()
        log.info("[IMAGE_SEARCH] execute_image_search: url='%s', query='%s'", resolved_url, clean_query)

        if not resolved_url:
            # 画像URLが見つからない場合、クエリがあればWeb検索へフォールバック
            if clean_query and not (clean_query.startswith("http://") or clean_query.startswith("https://")):
                from ..web_research import search_web_entries_unified

                entries = await search_web_entries_unified(clean_query, max_results=3)
                if entries:
                    lines = [
                        f"- [{e.get('title', '無題')}](<{e.get('url', '')}>): {e.get('snippet', '')}"
                        for e in entries
                    ]
                    out_text = (
                        f"画像URLが指定されていないため、キーワード '{clean_query}' でWeb検索しました:\n"
                        + "\n".join(lines)
                    )
                    log.info("[IMAGE_SEARCH] execute_image_search finished: success=True, source='web_search'")
                    return ToolResult(
                        name="search_image",
                        success=True,
                        output=out_text,
                        metadata={"fallback": "web_search", "query": clean_query},
                    )
            log.info("[IMAGE_SEARCH] execute_image_search finished: success=False, source='no_image_url'")
            return ToolResult(
                name="search_image",
                success=False,
                output="画像URLが指定されておらず、直前の会話や添付ファイルからも有効な画像URLを特定できませんでした。画像URLを指定するか画像を添付してください。",
                metadata={"error": "no_image_url"},
            )

        res = await cls.search_image_unified(resolved_url, query=clean_query, **kwargs)
        log.info("[IMAGE_SEARCH] execute_image_search finished: success=%s, source='%s'", res.get("success"), res.get("source"))
        return ToolResult(
            name="search_image",
            success=res.get("success", False),
            output=res.get("text", "検索結果を取得できませんでした。"),
            metadata=res,
        )

    @classmethod
    async def search_image_unified(
        cls,
        image_url: str,
        query: str = "",
        *,
        timeout_sec: float = 35.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Chrome MCP (Google Lens) -> HTTP直接検索 (Web検索) の多重防御画像検索"""
        log.info("[IMAGE_SEARCH] Starting unified image search: url='%s', query='%s'", image_url, query)
        encoded_url = urllib.parse.quote(image_url, safe="")
        lens_url = f"https://lens.google.com/uploadbyurl?url={encoded_url}"

        # 1. Chrome MCP (Google Lens) 最優先
        try:
            client = _get_shared_mcp_client()
        except Exception as e:
            client = None
            log.debug("[image_search] Could not get shared MCP client: %r", e)

        if client and getattr(client, "is_configured", True):
            try:
                log.info("[IMAGE_SEARCH] Attempting Google Lens via Chrome MCP: lens_url='%s'", lens_url)
                async with _browser_operation_guard():
                    # MCPツールマップの構築
                    tool_map: dict[str, dict[str, Any]] = {}
                    if hasattr(client, "list_tools"):
                        try:
                            tools = await client.list_tools()
                            for t in tools:
                                if isinstance(t, dict) and t.get("name"):
                                    tool_map[t["name"]] = t
                                elif hasattr(t, "name"):
                                    tool_map[t.name] = getattr(t, "schema", {}) or {}
                        except Exception as e:
                            log.debug("[image_search] list_tools error: %r", e)

                    nav_tool = "chrome_navigate" if "chrome_navigate" in tool_map else "navigate"
                    try:
                        nav_args = _build_navigate_args(tool_map, lens_url, prefer_new_window=True)
                    except TypeError:
                        try:
                            nav_args = _build_navigate_args(lens_url)  # type: ignore
                        except Exception:
                            nav_args = {"url": lens_url}

                    nav_res = await client.call_tool(nav_tool, nav_args)
                    tab_id = _extract_tab_id(nav_res) if callable(_extract_tab_id) else None

                    try:
                        await _sleep_after_browser_navigation(lens_url, reason="google_lens")
                    except Exception:
                        pass

                    content_res: Any = None
                    try:
                        try:
                            content_res = await _read_browser_content_with_retries(
                                client,
                                tool_map,
                                url=lens_url,
                                tab_id=tab_id,
                                preferred_format="text",
                                reject_login_wall=False,
                            )
                        except TypeError:
                            content_res = await _read_browser_content_with_retries(client, timeout_sec=15.0)  # type: ignore
                    finally:
                        try:
                            should_close = False
                            if callable(_should_close_browser_tab_after_navigation):
                                should_close = bool(_should_close_browser_tab_after_navigation(nav_res))
                            if should_close:
                                if callable(_close_browser_tab_if_possible):
                                    await _close_browser_tab_if_possible(client, tool_map, tab_id)
                                else:
                                    await client.call_tool("close_tab", {})
                        except Exception as e:
                            log.debug("[image_search] Close tab cleanup error: %r", e)

                    text_content = ""
                    if isinstance(content_res, dict):
                        text_content = str(content_res.get("text") or content_res.get("content") or "").strip()
                        if not text_content:
                            text_content = _extract_text_from_tool_result(content_res)
                    elif isinstance(content_res, str):
                        text_content = content_res.strip()

                    log.info("[IMAGE_SEARCH] Chrome MCP Google Lens fetched raw_chars=%d", len(text_content))

                    if text_content and len(text_content) >= 30 and not _is_lens_error_or_wall(text_content):
                        formatted = cls._format_lens_result(text_content, image_url, lens_url)
                        if formatted:
                            res = {
                                "success": True,
                                "source": "google_lens_mcp",
                                "text": formatted,
                                "raw_chars": len(text_content),
                                "lens_url": lens_url,
                            }
                            log.info("[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s'", res.get("success"), res.get("source"))
                            return res

                    log.info("[IMAGE_SEARCH] Google Lens returned error/wall or no matches, falling back to Web search")
            except Exception as e:
                log.warning("[IMAGE_SEARCH] Chrome MCP Google Lens search failed: %r, trying HTTP fallback", e)

        # 2. HTTP直接検索フォールバック（Web検索連携）
        fallback_query = _clean_fallback_query(query, image_url)
        if not fallback_query:
            log.info("[IMAGE_SEARCH] No meaningful fallback query found for image_url='%s'", image_url)
            res = {
                "success": False,
                "source": "failed",
                "text": (
                    f"画像検索を実行しましたが、Google Lens から一致結果を取得できませんでした。"
                    f"また、テキスト検索用の手掛かりとなるキーワードやファイル名も見つかりませんでした（検索URL: {lens_url}）。"
                ),
                "lens_url": lens_url,
                "raw_chars": 0,
            }
            log.info("[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s'", res.get("success"), res.get("source"))
            return res

        log.info("[IMAGE_SEARCH] Starting Web search fallback: fallback_query='%s'", fallback_query)
        try:
            from ..web_research import search_web_entries_unified

            entries = await search_web_entries_unified(fallback_query, max_results=3)
            if entries:
                lines = [
                    f"- [{e.get('title', '無題')}](<{e.get('url', '')}>): {e.get('snippet', '')}"
                    for e in entries
                ]
                res = {
                    "success": True,
                    "source": "web_fallback",
                    "text": f"Google Lens 直接取得に代わりキーワード '{fallback_query}' でWeb情報を検索しました:\n" + "\n".join(lines),
                    "lens_url": lens_url,
                    "raw_chars": sum(len(e.get("snippet", "")) for e in entries),
                }
                log.info("[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s'", res.get("success"), res.get("source"))
                return res
        except Exception as e:
            log.warning("[IMAGE_SEARCH] HTTP fallback failed: %r", e)

        res = {
            "success": False,
            "source": "failed",
            "text": f"画像検索を実行しましたが、有効な一致結果を取得できませんでした（検索URL: {lens_url}）。",
            "lens_url": lens_url,
            "raw_chars": 0,
        }
        log.info("[IMAGE_SEARCH] Unified image search completed: success=%s, source='%s'", res.get("success"), res.get("source"))
        return res

    @classmethod
    def _format_lens_result(cls, raw_text: str, image_url: str, lens_url: str) -> str:
        """Google Lens のブラウザテキストから主要な一致候補とタイトルを抽出・整形する"""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        # 「視覚的一致」「ビジュアル マッチ」等のマーカー以降を優先探索
        start_idx = 0
        for i, line in enumerate(lines):
            lowered = line.lower()
            if any(m in lowered for m in ("視覚的一致", "ビジュアル マッチ", "ビジュアルマッチ", "visual matches")):
                start_idx = i + 1
                break

        target_lines = lines[start_idx:] if start_idx > 0 else lines

        ui_noise_words = (
            "google",
            "ログイン",
            "プライバシー",
            "利用規約",
            "フィードバック",
            "画像で検索",
            "すべての画像",
            "すべての結果",
            "ツール",
            "共有",
            "送信",
            "詳細",
            "ヘルプ",
            "設定",
            "メニュー",
            "閉じる",
            "ビジュアル マッチ",
            "ビジュアルマッチ",
            "visual matches",
        )

        clean_lines: list[str] = []
        for l in target_lines:
            lowered = l.lower()
            # 単独のUI語句（1語）
            if lowered in ("検索", "画像", "すべての画像", "ツール", "共有", "詳細", "戻る", "メニュー", "閉じる"):
                continue
            # 部分一致ノイズ
            if any(noise in lowered for noise in ui_noise_words):
                continue
            # 極端に短い記号・1文字行
            if len(l) <= 1:
                continue
            clean_lines.append(l)

        if not clean_lines:
            return ""

        summary = "\n".join(clean_lines[:15])
        return (
            f"### 🔍 画像検索結果 (Google Lens)\n"
            f"- **対象画像**: {image_url}\n"
            f"- **Lens検索URL**: {lens_url}\n"
            f"**【検出された視覚的一致・関連テキスト】**:\n{summary}"
        )
