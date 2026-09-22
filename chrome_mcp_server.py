import asyncio
import json
import logging
from typing import Any, Dict
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, HTTPException
import uvicorn
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

app = FastAPI()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mcp_server")

browser: Browser | None = None
playwright_context = None

@app.on_event("startup")
async def startup():
    global browser, playwright_context
    logger.info("Starting up Playwright...")
    playwright_context = await async_playwright().start()
    browser = await playwright_context.chromium.launch(headless=True)
    logger.info("Playwright browser launched.")

@app.on_event("shutdown")
async def shutdown():
    global browser, playwright_context
    logger.info("Shutting down Playwright...")
    if browser:
        await browser.close()
    if playwright_context:
        await playwright_context.stop()

# Maps a mock tabId to a playwright Page to emulate stateful tabs
pages: Dict[int, Page] = {}
next_tab_id = 1


def _format_search_results_text(results: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for result in results:
        title = str(result.get("title") or "").strip()
        snippet = str(result.get("snippet") or "").strip()
        url = str(result.get("url") or "").strip()
        parts: list[str] = []
        if title:
            parts.append(title)
        if snippet:
            parts.append(snippet)
        if url:
            parts.append(f"URL: {url}")
        if parts:
            lines.append("- " + " / ".join(parts))
    return "\n".join(lines)


async def _search_with_browser(query: str, max_results: int) -> dict[str, Any]:
    if browser is None:
        raise RuntimeError("Playwright browser is not ready")
    max_results = max(1, min(int(max_results or 1), 10))
    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}&kl=jp-jp"
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        try:
            await page.wait_for_selector("a.result__a, .result, article, h2 a", timeout=5000)
        except Exception:
            pass
        results = await page.evaluate(
            """(maxResults) => {
                const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                const unwrapUrl = (href) => {
                    try {
                        const parsed = new URL(href, location.href);
                        const uddg = parsed.searchParams.get("uddg");
                        return clean(uddg || parsed.href);
                    } catch {
                        return clean(href);
                    }
                };
                const findSnippet = (container) => {
                    const selectors = [
                        ".result__snippet",
                        ".result__body",
                        "[data-result='snippet']",
                        "[data-testid='result-snippet']",
                        ".b_caption p",
                        "p"
                    ];
                    for (const selector of selectors) {
                        const node = container.querySelector(selector);
                        const text = clean(node && node.innerText);
                        if (text) return text;
                    }
                    return "";
                };
                const containers = Array.from(document.querySelectorAll(
                    ".result, .web-result, article, li[data-layout], div[data-testid='result']"
                ));
                const fallbackLinks = Array.from(document.querySelectorAll(
                    "a.result__a, a[data-testid='result-title-a'], h2 a"
                )).map((link) => link.closest(".result, .web-result, article, li[data-layout], div[data-testid='result']") || link);
                const candidates = containers.length ? containers : fallbackLinks;
                const seen = new Set();
                const results = [];
                for (const container of candidates) {
                    const link = container.matches && container.matches("a") ? container : container.querySelector(
                        "a.result__a, a[data-testid='result-title-a'], h2 a, a"
                    );
                    if (!link) continue;
                    const title = clean(link.innerText || link.textContent);
                    const url = unwrapUrl(link.href || link.getAttribute("href") || "");
                    if (!title || !url || seen.has(url)) continue;
                    seen.add(url);
                    results.push({title, snippet: findSnippet(container), url});
                    if (results.length >= maxResults) break;
                }
                return results;
            }""",
            max_results,
        )
        if not isinstance(results, list):
            results = []
        normalized_results: list[dict[str, str]] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            normalized_results.append({
                "title": str(result.get("title") or "").strip(),
                "snippet": str(result.get("snippet") or "").strip(),
                "url": str(result.get("url") or "").strip(),
            })
        return {"searchUrl": search_url, "results": normalized_results[:max_results]}
    finally:
        await context.close()

@app.post("/mcp")
async def handle_mcp(request: Request):
    """
    Simulates the MCP JSON-RPC protocol endpoint expected by the Discord Bot.
    """
    payload = await request.json()
    method = payload.get("method")
    req_id = payload.get("id")

    logger.info(f"Received MCP request: {method} id={req_id}")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}}
            }
        }
    
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "chrome_navigate",
                        "description": "Navigates to a URL",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"],
                            "additionalProperties": False
                        }
                    },
                    {
                        "name": "chrome_get_web_content",
                        "description": "Extracts text from a tab or URL",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "tabId": {"type": "integer"},
                                "url": {"type": "string"}
                            },
                            "additionalProperties": False
                        }
                    },
                    {
                        "name": "chrome_search",
                        "description": "Searches the web using a headless Chrome page and returns result entries",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "maxResults": {"type": "integer"}
                            },
                            "required": ["query"],
                            "additionalProperties": False
                        }
                    }
                ]
            }
        }
    
    elif method == "tools/call":
        params = payload.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        logger.info(f"Tool call: {tool_name} with args {args}")

        if tool_name == "chrome_navigate":
            url = args.get("url")
            if not url:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing url"}}
            
            global next_tab_id
            tab_id = next_tab_id
            next_tab_id += 1
            
            # Open a new context & page
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                logger.info(f"Navigated to {url} on tab {tab_id}")
            except Exception as e:
                logger.error(f"Navigation error for {url}: {e}")
                # We still return the tab ID, get_web_content might get page loaded partway or empty
            
            pages[tab_id] = page
            
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [
                        {"type": "text", "text": json.dumps({"tabId": tab_id, "url": url})}
                    ]
                }
            }

        elif tool_name == "chrome_get_web_content":
            tab_id = args.get("tabId")
            url = args.get("url")
            
            html = ""
            title = "(No Title)"
            page_url = url or ""
            text_content = ""
            
            if tab_id and tab_id in pages:
                page = pages[tab_id]
                try:
                    title = await page.title()
                    page_url = page.url
                    # Fast extraction of readable text using page eval
                    # Hides script/style elements and extracts body innerText
                    text_content = await page.evaluate('''() => {
                        const clone = document.body.cloneNode(true);
                        const elementsToRemove = clone.querySelectorAll('script, style, nav, footer, noscript, iframe');
                        elementsToRemove.forEach(e => e.remove());
                        return clone.innerText || "";
                    }''')
                except Exception as e:
                    logger.error(f"Content extraction error (tabId={tab_id}): {e}")
                finally:
                    await page.context.close()
                    del pages[tab_id]

            elif url:
                # Fallback: Navigate on the fly if only url is provided
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    title = await page.title()
                    page_url = page.url
                    text_content = await page.evaluate('''() => {
                        const clone = document.body.cloneNode(true);
                        const elementsToRemove = clone.querySelectorAll('script, style, nav, footer, noscript, iframe');
                        elementsToRemove.forEach(e => e.remove());
                        return clone.innerText || "";
                    }''')
                except Exception as e:
                    logger.error(f"Content fallback execution error for URL {url}: {e}")
                finally:
                    await context.close()
            else:
                 return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing tabId or url"}}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": text_content}],
                    "structuredContent": {
                        "title": title,
                        "url": page_url,
                        "textContent": text_content
                    }
                }
            }

        elif tool_name == "chrome_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing query"}}

            try:
                max_results = int(args.get("maxResults") or 5)
            except Exception:
                max_results = 5

            try:
                search_result = await _search_with_browser(query, max_results)
            except Exception as e:
                logger.error(f"Search execution error for query {query!r}: {e}")
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": f"Search failed: {e}"}}

            results = search_result.get("results", [])
            text_content = _format_search_results_text(results)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": text_content}],
                    "structuredContent": {
                        "query": query,
                        "searchUrl": search_result.get("searchUrl", ""),
                        "results": results,
                    }
                }
            }
        
        else:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Tool {tool_name} not found"}}
    
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": f"Unsupported method {method}"}}

if __name__ == "__main__":
    logger.info("Starting Chrome MCP Proxy Server on port 12306...")
    uvicorn.run(app, host="127.0.0.1", port=12306)
