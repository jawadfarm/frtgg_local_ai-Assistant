import httpx
import asyncio
import socket
import threading
import json
import logging
from typing import List
from dataclasses import dataclass
from bs4 import BeautifulSoup
import urllib.parse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("search_api.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DuckDuckGoAPI")


# A realistic browser User-Agent reduces the chance of being blocked.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int


class SimpleDuckDuckGo:

    def __init__(self):
        # html.duckduckgo.com is the primary scrape-friendly endpoint.
        # lite.duckduckgo.com is used as a fallback when the primary one
        # returns no parsable results (rate limit / layout change).
        self.html_url = "https://html.duckduckgo.com/html/"
        self.lite_url = "https://lite.duckduckgo.com/lite/"
        self.headers = DEFAULT_HEADERS
        # follow_redirects=True is critical: without it, any 301/302 makes
        # raise_for_status() throw and the request silently returns nothing.
        self.client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=self.headers,
        )

    @staticmethod
    def _clean_ddg_link(link: str) -> str:
        # DuckDuckGo wraps result links in a redirect like
        # //duckduckgo.com/l/?uddg=<encoded-real-url>&...
        if link and "uddg=" in link:
            link = urllib.parse.unquote(link.split("uddg=")[1].split("&")[0])
        if link.startswith("//"):
            link = "https:" + link
        return link

    def _parse_html_results(self, html: str, max_results: int) -> List[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: List[SearchResult] = []

        for item in soup.select(".result"):
            if len(results) >= max_results:
                break

            title_elem = item.select_one(".result__title a")
            if not title_elem:
                continue

            title = title_elem.get_text(strip=True)
            link = self._clean_ddg_link(title_elem.get("href", ""))

            snippet_elem = item.select_one(".result__snippet")
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            results.append(SearchResult(
                title=title,
                url=link,
                snippet=snippet,
                position=len(results) + 1
            ))

        return results

    def _parse_lite_results(self, html: str, max_results: int) -> List[SearchResult]:
        # The lite endpoint renders results as a flat table of <a> tags.
        soup = BeautifulSoup(html, "html.parser")
        results: List[SearchResult] = []

        for link_elem in soup.select("a.result-link"):
            if len(results) >= max_results:
                break

            title = link_elem.get_text(strip=True)
            link = self._clean_ddg_link(link_elem.get("href", ""))
            if not title or not link:
                continue

            results.append(SearchResult(
                title=title,
                url=link,
                snippet="",
                position=len(results) + 1
            ))

        return results

    async def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        # 1) Try the primary HTML endpoint.
        try:
            data = {"q": query, "b": "", "kl": ""}
            response = await self.client.post(self.html_url, data=data)
            response.raise_for_status()
            results = self._parse_html_results(response.text, max_results)
            if results:
                return results
            logger.warning("Primary endpoint returned 0 results, trying lite fallback")
        except Exception as e:
            logger.error(f"Search error (primary): {e}")

        # 2) Fallback to the lite endpoint.
        try:
            response = await self.client.post(self.lite_url, data={"q": query})
            response.raise_for_status()
            return self._parse_lite_results(response.text, max_results)
        except Exception as e:
            logger.error(f"Search error (fallback): {e}")
            return []

    async def fetch_webpage(self, url: str, max_chars: int = 5000) -> str:
        try:
            response = await self.client.get(url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            text = soup.get_text()
            lines = (line.strip() for line in text.splitlines())
            text = " ".join(line for line in lines if line)

            return text[:max_chars]

        except Exception as e:
            return f"Error fetching page: {str(e)}"

    async def close(self):
        await self.client.aclose()

    async def search_with_content(self, query: str, max_results: int, max_chars: int) -> List[dict]:
        results = await self.search(query, max_results)

        async def fetch_one(result: SearchResult) -> dict:
            content = await self.fetch_webpage(result.url, max_chars)
            return {
                "position": result.position,
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "content": content
            }

        output = await asyncio.gather(*(fetch_one(r) for r in results))
        return list(output)



async def deepSearch(query: str) -> dict:
    logger.info(f"deepSearch | query='{query}' | sites=20 | chars=1000")
    searcher = SimpleDuckDuckGo()
    try:
        results = await searcher.search_with_content(query, max_results=20, max_chars=1000)
        logger.info(f"deepSearch | done | found={len(results)}")
        return {"status": "ok", "mode": "deepSearch", "query": query, "total": len(results), "results": results}
    except Exception as e:
        logger.error(f"deepSearch error: {e}")
        return {"status": "error", "mode": "deepSearch", "message": str(e)}
    finally:
        await searcher.close()


async def normalSearch(query: str) -> dict:
    logger.info(f"normalSearch | query='{query}' | sites=10 | chars=500")
    searcher = SimpleDuckDuckGo()
    try:
        results = await searcher.search_with_content(query, max_results=10, max_chars=500)
        logger.info(f"normalSearch | done | found={len(results)}")
        return {"status": "ok", "mode": "normalSearch", "query": query, "total": len(results), "results": results}
    except Exception as e:
        logger.error(f"normalSearch error: {e}")
        return {"status": "error", "mode": "normalSearch", "message": str(e)}
    finally:
        await searcher.close()


async def fastSearch(query: str) -> dict:
    logger.info(f"fastSearch | query='{query}' | sites=5 | chars=300")
    searcher = SimpleDuckDuckGo()
    try:
        results = await searcher.search_with_content(query, max_results=5, max_chars=300)
        logger.info(f"fastSearch | done | found={len(results)}")
        return {"status": "ok", "mode": "fastSearch", "query": query, "total": len(results), "results": results}
    except Exception as e:
        logger.error(f"fastSearch error: {e}")
        return {"status": "error", "mode": "fastSearch", "message": str(e)}
    finally:
        await searcher.close()



async def deepFetch(url: str) -> dict:
    logger.info(f"deepFetch | url='{url}' | chars=5000")
    searcher = SimpleDuckDuckGo()
    try:
        content = await searcher.fetch_webpage(url, max_chars=5000)
        logger.info(f"deepFetch | done | url='{url}'")
        return {"status": "ok", "mode": "deepFetch", "url": url, "chars": 5000, "content": content}
    except Exception as e:
        logger.error(f"deepFetch error: {e}")
        return {"status": "error", "mode": "deepFetch", "message": str(e)}
    finally:
        await searcher.close()


async def normalFetch(url: str) -> dict:
    logger.info(f"normalFetch | url='{url}' | chars=2000")
    searcher = SimpleDuckDuckGo()
    try:
        content = await searcher.fetch_webpage(url, max_chars=2000)
        logger.info(f"normalFetch | done | url='{url}'")
        return {"status": "ok", "mode": "normalFetch", "url": url, "chars": 2000, "content": content}
    except Exception as e:
        logger.error(f"normalFetch error: {e}")
        return {"status": "error", "mode": "normalFetch", "message": str(e)}
    finally:
        await searcher.close()


async def fastFetch(url: str) -> dict:
    logger.info(f"fastFetch | url='{url}' | chars=500")
    searcher = SimpleDuckDuckGo()
    try:
        content = await searcher.fetch_webpage(url, max_chars=500)
        logger.info(f"fastFetch | done | url='{url}'")
        return {"status": "ok", "mode": "fastFetch", "url": url, "chars": 500, "content": content}
    except Exception as e:
        logger.error(f"fastFetch error: {e}")
        return {"status": "error", "mode": "fastFetch", "message": str(e)}
    finally:
        await searcher.close()


# =========== Command handler ===========

async def handle_command(request: dict) -> dict:
    """
        {"command": "fastSearch",   "query": "python"}
        {"command": "normalSearch", "query": "AI news"}
        {"command": "deepSearch",   "query": "machine learning"}
        {"command": "fastFetch",    "url": "https://example.com"}
        {"command": "normalFetch",  "url": "https://example.com"}
        {"command": "deepFetch",    "url": "https://example.com"}
    """
    command = request.get("command", "")
    logger.info(f"handle_command | command='{command}'")

    search_commands = {
        "fastSearch":   fastSearch,
        "normalSearch": normalSearch,
        "deepSearch":   deepSearch,
    }
    fetch_commands = {
        "fastFetch":   fastFetch,
        "normalFetch": normalFetch,
        "deepFetch":   deepFetch,
    }

    if command in search_commands:
        query = request.get("query", "")
        if not query:
            return {"status": "error", "message": "missing 'query' field"}
        return await search_commands[command](query)

    elif command in fetch_commands:
        url = request.get("url", "")
        if not url:
            return {"status": "error", "message": "missing 'url' field"}
        return await fetch_commands[command](url)

    else:
        logger.warning(f"unknown command: '{command}'")
        return {
            "status": "error",
            "message": f"Unknown command: '{command}'",
            "available_commands": list(search_commands.keys()) + list(fetch_commands.keys())
        }


# =========== Socket Server ===========

class SearchSocketServer:


    def __init__(self, host: str = "0.0.0.0", port: int = 8400):
        self.host = host
        self.port = port
        self.loop = asyncio.new_event_loop()

    def _handle_client(self, conn: socket.socket, addr):
        try:
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)

            raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
            logger.info(f"Received from {addr}: {raw[:300]}")

            try:
                request = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error(f"JSON parse error from {addr}: {e}")
                response = {"status": "error", "message": f"Invalid JSON: {str(e)}"}
                conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                return

            # Run the command on the asyncio event loop.
            result = asyncio.run_coroutine_threadsafe(
                handle_command(request),
                self.loop
            ).result(timeout=120)

            response_json = json.dumps(result, ensure_ascii=False)
            conn.sendall(response_json.encode("utf-8"))
            logger.info(f"Response sent to {addr} | status={result.get('status')} | size={len(response_json)} chars")

        except Exception as e:
            logger.error(f"Client handler error ({addr}): {e}")
            try:
                error_resp = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
                conn.sendall(error_resp.encode("utf-8"))
            except Exception:
                pass
        finally:
            conn.close()

    def run(self):
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        # Set up the listening socket.
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(10)

        logger.info(f"Search Socket Server running on {self.host}:{self.port}")
        logger.info("Available commands: fastSearch, normalSearch, deepSearch, fastFetch, normalFetch, deepFetch")

        while True:
            try:
                conn, addr = server_socket.accept()
                logger.info(f"New connection from {addr}")
                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    daemon=True
                ).start()
            except Exception as e:
                logger.error(f"Server accept error: {e}")


# =========== Main ===========

if __name__ == "__main__":
    import sys

    # Quick manual test without the socket server:
    #   python internetapi.py test fastSearch "python tutorial"
    #   python internetapi.py test fastFetch  "https://example.com"
    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        command = sys.argv[2]
        arg = sys.argv[3] if len(sys.argv) > 3 else ""
        req = {"command": command}
        if command.endswith("Search"):
            req["query"] = arg
        else:
            req["url"] = arg
        out = asyncio.run(handle_command(req))
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        server = SearchSocketServer(host="0.0.0.0", port=8400)
        server.run()
