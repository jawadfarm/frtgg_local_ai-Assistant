# -*- coding: utf-8 -*-
"""
FARM HARVEST - CLIENT
Port 8716 — receives text commands, forwards to Farm Harvest server on 8901
"""

import socket
import json
import threading
import sys
import re

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8901
CLIENT_HOST = "0.0.0.0"
CLIENT_PORT = 8716

# ─────────────────────────────────────────────
#  AI OUTPUT SETTINGS
# ─────────────────────────────────────────────
MAX_AI_CHARS      = 10_000   # Hard cap on total output sent to AI
MAX_SECTION_CHARS = 4_000    # Cap per section (html, text, js, etc.)

BOOL_TRUE  = {"true", "yes", "1"}
BOOL_FALSE = {"false", "no", "0"}

# ─────────────────────────────────────────────
#  COOKIE FILTER
# ─────────────────────────────────────────────
BORING_COOKIES = {
    "__Secure-STRP",
    "__Secure-BUCKET",
    "SOCS",
    "CONSENT",
    "ANID",
}

def filter_cookies(cookies: dict) -> dict:
    return {k: v for k, v in cookies.items() if k not in BORING_COOKIES}


# ─────────────────────────────────────────────
#  TEXT SANITIZER  (removes non-ASCII noise)
# ─────────────────────────────────────────────
def sanitize(text: str, max_chars: int = None) -> str:
    """
    Removes non-ASCII characters that confuse the LLM,
    then optionally truncates to max_chars.
    """
    # Keep only printable ASCII + common whitespace
    cleaned = re.sub(r'[^\x09\x0A\x0D\x20-\x7E]', '', text)
    # Collapse excessive blank lines
    cleaned = re.sub(r'\n{4,}', '\n\n\n', cleaned)
    if max_chars and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
        cleaned += f"\n\n[... TRUNCATED — original was longer. Showing first {max_chars} chars ...]"
    return cleaned


def truncate_notice(original_len: int, shown_len: int) -> str:
    return (
        f"\n[NOTE FOR AI] The data above was TRUNCATED. "
        f"Original size: {original_len} chars. "
        f"Shown: {shown_len} chars. "
        f"Analyze only what is shown — do not assume the rest is identical."
    )


# ─────────────────────────────────────────────
#  COMMAND ALIASES
# ─────────────────────────────────────────────
COMMAND_MAP = {
    "reconweb":   "fetch",
    "fetch":      "fetch",
    "lasturls":   "last_urls",
    "last_urls":  "last_urls",
    "subdomains": "subdomains",
    "help":       "help",
}

PRIMARY_KEY = {
    "fetch":      "url",
    "last_urls":  None,
    "subdomains": "domain",
    "help":       None,
}


# ─────────────────────────────────────────────
#  RESPONSE FORMATTER  (for AI readability)
# ─────────────────────────────────────────────
def format_for_ai(result: dict, command: str, payload: dict = None) -> str:
    out = []
    status = result.get("status", "unknown")

    if payload is None:
        payload = {}

    show_cookies   = payload.get("show_cookies",   True)
    show_html      = payload.get("show_html",      True)
    show_links     = payload.get("show_links",     True)
    show_all_links = payload.get("show_all_links", False)
    show_js        = payload.get("show_js",        True)
    show_text      = payload.get("show_text",      True)

    if status == "error":
        return f"[ERROR] {result.get('message', 'unknown error')}"

    # ── Website fetch ──
    if "request" in result:
        req  = result.get("request", {})
        resp = result.get("response", {})
        out.append(f"URL: {req.get('url', '-')}")
        out.append(f"HTTP: {resp.get('status_code')} {resp.get('reason')}")

        # ── Headers ──
        headers = resp.get("headers", {})
        if headers:
            out.append("\n--- HEADERS ---")
            for k, v in headers.items():
                line = sanitize(f"  {k}: {v}")
                out.append(line)

        # ── Cookies ──
        if show_cookies:
            cookies = resp.get("cookies", {})
            if cookies:
                filtered = filter_cookies(cookies)
                out.append("\n--- COOKIES ---")
                for k, v in filtered.items():
                    out.append(sanitize(f"  {k}: {v}"))

    # ── Links ──
    if "links" in result and show_links:
        lnk = result["links"]
        out.append(
            f"\n--- LINKS ---\n"
            f"Total: {lnk['total']} | Internal: {lnk['internal_count']} | External: {lnk['external_count']}"
        )

        internal = lnk["internal"]
        external = lnk["external"]

        out.append("Internal:")
        for u in (internal if show_all_links else internal[:5]):
            out.append(f"  {sanitize(u)}")
        if not show_all_links and len(internal) > 5:
            out.append(f"  ... and {len(internal) - 5} more (use show_all_links=True to see all)")

        out.append("External:")
        for u in (external if show_all_links else external[:5]):
            out.append(f"  {sanitize(u)}")
        if not show_all_links and len(external) > 5:
            out.append(f"  ... and {len(external) - 5} more")

    # ── Visible text ──
    if "text_content" in result and show_text:
        raw_text = result["text_content"]
        clean    = sanitize(raw_text, MAX_SECTION_CHARS)
        out.append(f"\n--- PAGE TEXT ---")
        out.append(clean)
        if len(raw_text) > MAX_SECTION_CHARS:
            out.append(truncate_notice(len(raw_text), MAX_SECTION_CHARS))

    # ── HTML ──
    if "html" in result and show_html:
        raw_html = result["html"]
        clean    = sanitize(raw_html, MAX_SECTION_CHARS)
        out.append(f"\n--- HTML SOURCE ---")
        out.append(clean)
        if len(raw_html) > MAX_SECTION_CHARS:
            out.append(truncate_notice(len(raw_html), MAX_SECTION_CHARS))

    # ── JavaScript ──
    if "javascript" in result and show_js:
        js      = result["javascript"]
        scripts = js.get("scripts", {})
        items   = list(scripts.items())
        out.append(f"\n--- JAVASCRIPT ({js.get('total', 0)} scripts) ---")
        js_budget = MAX_SECTION_CHARS
        for i, (idx, script) in enumerate(items):
            if js_budget <= 0:
                out.append(f"  [... remaining {len(items) - i} scripts omitted — budget exhausted ...]")
                break
            snippet = sanitize(script["content"], min(js_budget, 800))
            js_budget -= len(snippet)
            out.append(f"Script {idx} | src: {sanitize(script['src'])}")
            out.append("<script>")
            out.append(snippet)
            out.append("</script>")
            if i < len(items) - 1:
                out.append("___")

    # ── Element search ──
    if "element_search" in result:
        es = result["element_search"]
        out.append(f"\n--- ELEMENTS FOUND ({es['count']}) ---")
        for el in es["elements"][:5]:
            out.append(f"  <{el['tag']}> {sanitize(el['text'][:80])}")

    # ── Subdomains ──
    if "subdomains" in result and isinstance(result["subdomains"], list):
        subs = result["subdomains"]
        out.append(f"\n--- SUBDOMAINS ({len(subs)} found) ---")
        for s in subs[:15]:
            out.append(f"  {sanitize(s)}")
        if len(subs) > 15:
            out.append(f"  ... and {len(subs) - 15} more")

    # ── Last URLs ──
    if "urls" in result:
        urls = result["urls"]
        out.append(f"\n--- LAST URLs ({result.get('showing', len(urls))}) ---")
        for idx, url in urls.items():
            out.append(f"  {idx}. {sanitize(url)}")

    # ─────────────────────────────────────────
    #  FINAL ASSEMBLY — enforce global cap
    # ─────────────────────────────────────────
    assembled = "\n".join(out) if out else "[OK]"

    if len(assembled) > MAX_AI_CHARS:
        cut      = assembled[:MAX_AI_CHARS]
        original = len(assembled)
        cut     += (
            f"\n\n{'='*60}\n"
            f"[WARNING FOR AI] OUTPUT TRUNCATED\n"
            f"Original output: {original:,} characters\n"
            f"Shown to you:    {MAX_AI_CHARS:,} characters\n"
            f"The data is very large. Analyze only what is shown above.\n"
            f"If you need a specific section, ask the user to re-run with targeted flags.\n"
            f"{'='*60}"
        )
        return cut

    return assembled


# ─────────────────────────────────────────────
#  VALUE PARSER
# ─────────────────────────────────────────────
def parse_value(v: str):
    v = v.strip()
    if v.lower() in BOOL_TRUE:  return True
    if v.lower() in BOOL_FALSE: return False
    try: return int(v)
    except ValueError: pass
    try: return float(v)
    except ValueError: pass
    return v


# ─────────────────────────────────────────────
#  COMMAND PARSER
# ─────────────────────────────────────────────
def parse_command(raw: str) -> dict:
    """
    Parse plain-text command into dict payload.

    Examples:
      reconweb:url:https://google.com,show_links=True,max_chars=3000
      reconweb:url:https://google.com,show_links=True,show_all_links=True
      reconweb:url:https://google.com,show_cookies=True
      lasturls:limit=5
      subdomains:domain:google.com
      help
    """
    raw   = raw.strip()
    parts = raw.split(":", 2)
    cmd_raw = parts[0].strip().lower()
    command = COMMAND_MAP.get(cmd_raw)

    if not command:
        return {"command": cmd_raw}

    payload = {"command": command}
    pk = PRIMARY_KEY.get(command)

    if len(parts) == 1:
        return payload

    if len(parts) == 2:
        for item in parts[1].split(","):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                payload[k.strip().lower()] = parse_value(v)
        return payload

    # len == 3
    rest        = parts[2]
    first_comma = rest.find(",")
    if first_comma == -1:
        primary_value = rest
        extras_str    = ""
    else:
        primary_value = rest[:first_comma]
        extras_str    = rest[first_comma + 1:]

    if pk:
        payload[pk] = parse_value(primary_value)

    for item in extras_str.split(","):
        item = item.strip()
        if item and "=" in item:
            k, v = item.split("=", 1)
            payload[k.strip().lower()] = parse_value(v)

    return payload


# ─────────────────────────────────────────────
#  SERVER SENDER
# ─────────────────────────────────────────────
def send_to_server(payload: dict) -> dict:
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(60)
        client.connect((SERVER_HOST, SERVER_PORT))
        client.send((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

        response = b""
        while True:
            try:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break

        client.close()
        return json.loads(response.decode("utf-8").strip())

    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────
#  CONNECTION HANDLER
# ─────────────────────────────────────────────
def handle(conn: socket.socket, addr):
    print(f"\n[Client] Connection from {addr}")
    try:
        raw = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk: break
            raw += chunk
            if b"\n" in raw: break

        text = raw.decode("utf-8", errors="replace").strip()
        print(f"[Client] Received : {text}")

        if not text:
            result   = {"status": "error", "message": "Empty command"}
            payload  = {}
            original = ""
        else:
            try:
                payload  = json.loads(text)
                payload  = {k.lower(): v for k, v in payload.items()}
                original = text
            except json.JSONDecodeError:
                payload  = parse_command(text)
                original = text

            print(f"[Client] Parsed   : {json.dumps(payload, ensure_ascii=False)}")
            result = send_to_server(payload)

        formatted = format_for_ai(result, original, payload)
        print(f"[Client] Result ({len(formatted)} chars):\n{formatted[:500]}{'...' if len(formatted) > 500 else ''}")

        out = (formatted + "\n").encode("utf-8")
        conn.sendall(out)

    except Exception as e:
        conn.sendall(f"[ERROR] {e}\n".encode())
    finally:
        conn.close()
        print(f"[Client] Closed   : {addr}")


# ─────────────────────────────────────────────
#  DIRECT PYTHON SENDER
# ─────────────────────────────────────────────
def sender(command: dict | str):
    if isinstance(command, str):
        payload  = parse_command(command)
        original = command
    else:
        payload  = {k.lower(): v for k, v in command.items()}
        original = str(command)

    result    = send_to_server(payload)
    formatted = format_for_ai(result, original, payload)
    print(formatted)
    return result


# ─────────────────────────────────────────────
#  CLIENT SERVER  (port 8716)
# ─────────────────────────────────────────────
def start_client_server():
    print("""
+------------------------------------------+
|      FARM HARVEST - CLIENT SERVER        |
|  Listening : 0.0.0.0:8716               |
|  Forwarding: 127.0.0.1:8901             |
+------------------------------------------+
Commands:
  reconweb:url:https://google.com,show_links=True,max_chars=3000
  reconweb:url:https://google.com,show_links=True,show_all_links=True
  reconweb:url:https://google.com,show_cookies=True
  lasturls:limit=5
  subdomains:domain:google.com
  help
""")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((CLIENT_HOST, CLIENT_PORT))
        srv.listen(10)
        print(f"[Client] Listening on {CLIENT_HOST}:{CLIENT_PORT} ...")

        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
            except KeyboardInterrupt:
                print("[Client] Shutting down.")
                break
            except Exception as e:
                print(f"[Client] Error: {e}")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1:
        sender(" ".join(sys.argv[1:]))
    else:
        start_client_server()