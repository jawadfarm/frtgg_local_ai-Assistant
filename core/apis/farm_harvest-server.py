# -*- coding: utf-8 -*-
"""
FARM HARVEST 3.0 - SERVER EDITION
Socket Server: 0.0.0.0:8901
JSON Responses | Logging | Help Command
"""

import os
import sys
import json
import socket
import threading
import logging
import requests
import bs4
import urllib.parse
import urllib3
import time
from datetime import datetime, timezone

datetime.now(timezone.utc)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
#  LOGGER SETUP
# ─────────────────────────────────────────────
LOG_FILE = "farm_harvest.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("FarmHarvest")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def ok(data: dict) -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat(), **data}

def err(msg: str) -> dict:
    return {"status": "error", "timestamp": datetime.now(timezone.utc).isoformat(), "message": msg}

def truncate(text: str, max_chars: int | None) -> str:
    if max_chars is None:
        return text
    return text[:max_chars]


# ─────────────────────────────────────────────
#  SUBDOMAIN FINDER
# ─────────────────────────────────────────────
class SubdomainFinder:
    def __init__(self, domain: str, save_file: str | None = None):
        self.domain = domain.lower().strip()
        self.save_file = save_file
        self.subdomains: list[str] = []

    def fetch(self) -> dict:
        url = f"https://crt.sh/?q={self.domain}&output=json"
        try:
            logger.info(f"[SubdomainFinder] Fetching subdomains for: {self.domain}")
            response = requests.get(url, timeout=60)
            if response.status_code != 200:
                return err(f"crt.sh returned HTTP {response.status_code}")
            data = response.json()
            self.subdomains = self._process(data)
            if self.save_file:
                self._save()
            logger.info(f"[SubdomainFinder] Found {len(self.subdomains)} subdomains")
            return ok({"domain": self.domain, "count": len(self.subdomains), "subdomains": self.subdomains})
        except Exception as e:
            logger.error(f"[SubdomainFinder] {e}")
            return err(str(e))

    def _process(self, data) -> list[str]:
        found = set()
        for item in data:
            name = item.get("name_value", "")
            if not name or name.startswith("*"):
                continue
            for line in name.split("\n"):
                line = line.strip().lower()
                if line.endswith(self.domain):
                    found.add(line)
        return sorted(found)

    def _save(self):
        try:
            with open(self.save_file, "a") as f:
                f.write("\n".join(self.subdomains) + "\n")
            logger.info(f"[SubdomainFinder] Saved to {self.save_file}")
        except Exception as e:
            logger.error(f"[SubdomainFinder] Save failed: {e}")


# ─────────────────────────────────────────────
#  CORE BODY (HTTP Fetcher)
# ─────────────────────────────────────────────
class Body:
    def extract_all_links(self, soup: bs4.BeautifulSoup, base_url: str) -> list[str]:
        links = set()
        for tag in soup.find_all(["a", "link", "area"], href=True):
            link = tag["href"]
            if link and not link.startswith(("javascript:", "mailto:", "tel:")):
                links.add(urllib.parse.urljoin(base_url, link))
        for tag in soup.find_all(["img", "script", "iframe", "embed", "source", "audio", "video"], src=True):
            links.add(urllib.parse.urljoin(base_url, tag["src"]))
        for tag in soup.find_all(srcset=True):
            for source in tag["srcset"].split(","):
                link = source.strip().split(" ")[0]
                if link:
                    links.add(urllib.parse.urljoin(base_url, link))
        for tag in soup.find_all("form", action=True):
            links.add(urllib.parse.urljoin(base_url, tag["action"]))
        import re
        for script in soup.find_all("script"):
            if script.string:
                for link in re.findall(r'["\'](https?://[^"\'\s]+)["\']', script.string):
                    links.add(link)
        return sorted(links)

    def farm_harvest(
        self,
        url: str,
        show_links: bool = False,
        show_html: bool = False,
        show_javascript: bool = False,
        only_texthtml: bool = False,
        show_content: bool = False,
        get_subdomains: bool = False,
        element_search: dict | None = None,
        headers: dict | None = None,
        verify_ssl: bool = True,
        max_chars: int | None = None,
        save_file: str | None = None,
    ) -> dict:
        logger.info(f"[FarmHarvest] Fetching URL: {url}")
        try:
            response = requests.get(url, headers=headers, verify=verify_ssl, timeout=30)
        except Exception as e:
            logger.error(f"[FarmHarvest] Request failed: {e}")
            return err(str(e))

        soup = bs4.BeautifulSoup(response.text, "html.parser")
        all_links = self.extract_all_links(soup, url)
        domain = urllib.parse.urlparse(url).netloc

        result: dict = {
            "request": {
                "url": url,
                "method": response.request.method,
            },
            "response": {
                "status_code": response.status_code,
                "reason": response.reason,
                "ok": response.ok,
                "is_redirect": response.is_redirect,
                "headers": dict(response.headers),
                "cookies": dict(response.cookies),
            },
        }

        if show_content:
            raw = response.content.decode("utf-8", errors="replace")
            result["content"] = truncate(raw, max_chars)

        if show_html:
            html_str = str(soup)
            result["html"] = truncate(html_str, max_chars)

        if only_texthtml:
            texts = []
            for el in soup.find_all(["h1", "h2", "h3", "p", "a", "span", "div"]):
                t = el.get_text(strip=True)
                if t:
                    texts.append({"tag": el.name, "text": t})
            full_text = " ".join(x["text"] for x in texts)
            result["text_elements"] = texts
            result["text_content"] = truncate(full_text, max_chars)

        if show_javascript:
            scripts = {}
            index = 1
            remaining = max_chars  # الحد الكلي المتبقي

            for s in soup.find_all("script"):
                if s.string:
                    # إذا انتهى الحد الكلي، وقف
                    if max_chars is not None and remaining <= 0:
                        break

                    src = s.get("src", "inline")
                    content = s.string

                    if max_chars is not None:
                        # خذ بس اللي تبقى من الحد الكلي
                        content = content[:remaining]
                        remaining -= len(content)

                    scripts[f"{index}"] = {
                        "src": src,
                        "content": content,
                        "length": len(s.string),
                    }
                    index += 1

            result["javascript"] = {
                "total": len(scripts),
                "scripts": scripts,
            }

        if show_links:
            internal = [l for l in all_links if urllib.parse.urlparse(l).netloc == domain]
            external = [l for l in all_links if urllib.parse.urlparse(l).netloc != domain]
            result["links"] = {
                "total": len(all_links),
                "internal_count": len(internal),
                "external_count": len(external),
                "internal": internal,
                "external": external,
            }

        if get_subdomains:
            d = domain.split(".")
            if len(d) > 2:
                d = d[-2:]
            base_domain = ".".join(d)
            sf = SubdomainFinder(base_domain, save_file)
            result["subdomains"] = sf.fetch()

        if element_search:
            etype = element_search.get("element_type")
            aname = element_search.get("attribute_name")
            avalue = element_search.get("attribute_value")
            attrs = {aname: avalue} if aname and avalue else {}
            elements = soup.find_all(etype, attrs=attrs) if etype else []
            found = []
            for el in elements:
                found.append({
                    "tag": el.name,
                    "text": el.get_text(strip=True),
                    "html": truncate(str(el), max_chars),
                })
            result["element_search"] = {"query": element_search, "count": len(found), "elements": found}

        if save_file:
            try:
                with open(save_file, "a") as f:
                    f.write(json.dumps(result, ensure_ascii=False) + "\n")
                logger.info(f"[FarmHarvest] Result saved to {save_file}")
            except Exception as e:
                logger.error(f"[FarmHarvest] Save error: {e}")

        logger.info(f"[FarmHarvest] Done. Status: {response.status_code}")
        return ok(result)


# ─────────────────────────────────────────────
#  COMMAND DISPATCHER
# ─────────────────────────────────────────────
HELP_TEXT = {
    "commands": {
        "fetch": {
            "description": "Fetch a URL and get response info.",
            "params": {
                "url": "(required) Target URL e.g. https://example.com",
                "show_links": "(bool) Extract all links from page",
                "show_html": "(bool) Return full HTML",
                "show_javascript": "(bool) Return all script tags",
                "only_texthtml": "(bool) Return only visible text elements",
                "show_content": "(bool) Return raw content",
                "get_subdomains": "(bool) Run subdomain finder on target domain",
                "verify_ssl": "(bool) Verify SSL certificate (default true)",
                "max_chars": "(int) Total character limit across ALL scripts combined",
                "headers": "(object) Custom request headers e.g. {'User-Agent': 'Bot'}",
                "save_file": "(str) File path to append results as JSON",
                "element_search": {
                    "description": "(object) Search for specific HTML elements",
                    "fields": {
                        "element_type": "HTML tag name e.g. div, a, input",
                        "attribute_name": "Attribute to filter by e.g. class",
                        "attribute_value": "Value of the attribute",
                    },
                },
            },
            "example": {
                "command": "fetch",
                "url": "https://example.com",
                "show_links": True,
                "max_chars": 5000,
            },
        },
        "subdomains": {
            "description": "Find subdomains for a given domain using crt.sh.",
            "params": {
                "domain": "(required) Root domain e.g. example.com",
                "save_file": "(str) Optional file to save results",
            },
            "example": {"command": "subdomains", "domain": "example.com"},
        },
        "trim": {
            "description": "Trim a block of text to N characters.",
            "params": {
                "text": "(required) The text to trim",
                "max_chars": "(required) Number of characters to keep",
            },
            "example": {"command": "trim", "text": "Hello World!", "max_chars": 5},
        },
        "last_urls": {
            "description": "Show the last N URLs you fetched (from urlback.txt).",
            "params": {
                "limit": "(int) How many URLs to show, default 10. Most recent first.",
            },
            "example": {"command": "last_urls", "limit": 5},
        },
        "help": {
            "description": "Show this help message.",
            "example": {"command": "help"},
        },
    },
    "usage": (
        "Send a JSON object over TCP to 0.0.0.0:8901. "
        "Each request must have a 'command' field. "
        "Responses are JSON. "
        "End each message with a newline '\\n'."
    ),
    "server": "0.0.0.0:8901",
    "log_file": LOG_FILE,
}


def dispatch(payload: dict) -> dict:
    command = payload.get("command", "").lower().strip()
    logger.info(f"[Dispatch] Command received: '{command}'")

    if command == "help":
        return ok({"help": HELP_TEXT})

    elif command == "fetch":
        url = payload.get("url")
        if not url:
            return err("'url' is required for fetch command")

        default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        user_headers = payload.get("headers") or {}
        merged_headers = {**default_headers, **user_headers}

        b = Body()
        return b.farm_harvest(
            url=url,
            show_links=payload.get("show_links", False),
            show_html=payload.get("show_html", False),
            show_javascript=payload.get("show_javascript", False),
            only_texthtml=payload.get("only_texthtml", False),
            show_content=payload.get("show_content", False),
            get_subdomains=payload.get("get_subdomains", False),
            element_search=payload.get("element_search"),
            headers=merged_headers,
            verify_ssl=payload.get("verify_ssl", True),
            max_chars=payload.get("max_chars"),
            save_file=payload.get("save_file"),
        )

    elif command == "subdomains":
        domain = payload.get("domain")
        if not domain:
            return err("'domain' is required for subdomains command")
        sf = SubdomainFinder(domain, payload.get("save_file"))
        return sf.fetch()

    elif command == "last_urls":
        try:
            limit = int(payload.get("limit", 10))
            with open("urlback.txt", "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            last = lines[-limit:] if limit else lines
            last_indexed = {str(i + 1): u for i, u in enumerate(reversed(last))}
            logger.info(f"[LastURLs] Showing last {len(last_indexed)} of {len(lines)} saved URLs")
            return ok({
                "total_saved": len(lines),
                "showing": len(last_indexed),
                "note": "1 = most recent",
                "urls": last_indexed,
            })
        except FileNotFoundError:
            return err("urlback.txt not found - no URLs saved yet")
        except Exception as e:
            logger.error(f"[LastURLs] {e}")
            return err(str(e))

    elif command == "trim":
        text = payload.get("text")
        max_chars = payload.get("max_chars")
        if text is None or max_chars is None:
            return err("'text' and 'max_chars' are required for trim command")
        trimmed = truncate(str(text), int(max_chars))
        return ok({
            "original_length": len(str(text)),
            "trimmed_length": len(trimmed),
            "max_chars": max_chars,
            "text": trimmed,
        })

    else:
        return err(f"Unknown command: '{command}'. Send {{\"command\":\"help\"}} for usage.")


# ─────────────────────────────────────────────
#  SOCKET SERVER
# ─────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 8901
BUFFER = 65536


def handle_client(conn: socket.socket, addr):
    logger.info(f"[Server] Connection from {addr}")
    try:
        raw = b""
        while True:
            chunk = conn.recv(BUFFER)
            if not chunk:
                break
            raw += chunk
            if b"\n" in raw:
                break

        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            response = err("Empty request")
        else:
            try:
                payload = json.loads(text)
                response = dispatch(payload)
            except json.JSONDecodeError as e:
                logger.warning(f"[Server] JSON parse error from {addr}: {e}")
                response = err(f"Invalid JSON: {e}")

        out = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
        conn.sendall(out)
        logger.info(f"[Server] Response sent to {addr} ({len(out)} bytes)")

    except Exception as e:
        logger.error(f"[Server] Error handling {addr}: {e}")
        try:
            conn.sendall((json.dumps(err(str(e))) + "\n").encode())
        except Exception:
            pass
    finally:
        conn.close()
        logger.info(f"[Server] Connection closed: {addr}")


def start_server():
    logger.info(f"[Server] Starting FARM HARVEST 3.0 on {HOST}:{PORT}")
    print(f"""
╔══════════════════════════════════════╗
║      FARM HARVEST 3.0 - SERVER       ║
║  Socket : {HOST}:{PORT}              ║
║  Log    : {LOG_FILE:<26}             ║
╚══════════════════════════════════════╝
Send JSON commands ending with \\n
Example: {{"command":"help"}}
""")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(10)
        logger.info(f"[Server] Listening on {HOST}:{PORT}")

        while True:
            try:
                conn, addr = srv.accept()
                t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                t.start()
            except KeyboardInterrupt:
                logger.info("[Server] Shutting down.")
                break
            except Exception as e:
                logger.error(f"[Server] Accept error: {e}")


if __name__ == "__main__":
    open("urlback.txt", "a").close()
    start_server()