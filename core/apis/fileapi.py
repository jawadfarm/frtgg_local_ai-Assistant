import sys
import os
import socket
import threading
import json
import shutil
import ipaddress

# Force UTF-8 on stdout/stderr so log prints containing Unicode (→, ─, …) don't
# raise UnicodeEncodeError on a cp1252 Windows console — whether run directly or
# as a piped subprocess under runner.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BLOCKS_FILE = ("file_blocks.json")
SETTINGS_FILE = "fileApi_settings.json"
DEFAULT_PORT = 8910

commands = ["readfile", "writefile", "delete", "addfile", "listfile", "get_folders", "extFiles", "size", "editline", "deleteline", "startwithline", "listall", "mkdir", "rmdir", "read_from", "edit_with-line"]

def load_settings():
    defaults = {"port": DEFAULT_PORT, "public": False, "private": False}
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    return {**defaults, **data}

def connection_allowed(ip, settings):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return bool(settings.get("public"))
    if addr.is_loopback:                       # localhost: always allowed
        return True
    if addr.is_private or addr.is_link_local:  # internal network (LAN)
        return bool(settings.get("private"))
    return bool(settings.get("public"))        # external / internet

def parse_range(spec):
    a, _, b = spec.partition("-")
    return int(a), int(b)

def load_blocks():
    if not os.path.exists(BLOCKS_FILE):
        return {}
    with open(BLOCKS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def sync_blocks():
    """Ensure file_blocks.json exists and lists every command.
    Creates the file if missing, and adds any new command with block=False
    without touching entries that already exist."""
    if os.path.exists(BLOCKS_FILE):
        try:
            with open(BLOCKS_FILE, "r", encoding="utf-8") as f:
                blocks = json.load(f) or {}
        except Exception:
            blocks = {}
    else:
        blocks = {}
    added = [cmd for cmd in commands if cmd not in blocks]
    for cmd in added:
        blocks[cmd] = {"block": False}
    if added or not os.path.exists(BLOCKS_FILE):
        with open(BLOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(blocks, f, indent=2, ensure_ascii=False)
        if added:
            print(f"[*] file_blocks.json: added {len(added)} command(s): {', '.join(added)}")
    return blocks

def is_blocked(cmd):
    blocks = load_blocks()
    if cmd in blocks:
        return blocks[cmd].get("block", False)
    return False

def extract_path_and_args(rest: str, num_args: int = 0):
    if num_args == 0:
        return rest.strip(), []
    parts = rest.rsplit(" ", num_args)
    return parts[0].strip(), parts[1:]

def runner(command, content=None):
    command = command.strip()

    first_space = command.find(" ")
    if first_space == -1:
        cmd = command
        rest = ""
    else:
        cmd = command[:first_space]
        rest = command[first_space + 1:].strip()

    if cmd not in commands:
        return {"status": "error", "cmd": cmd, "message": f"Unknown command: {cmd}"}

    if is_blocked(cmd):
        return {"status": "error", "cmd": cmd, "message": "you cant use this command Access is denied"}

    try:
        if cmd == "readfile":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8") as f:
                return {"status": "ok", "cmd": cmd, "path": path, "content": f.read()}

        elif cmd == "writefile":
            path, _ = extract_path_and_args(rest, 0)
            text = content if content is not None else ""
            print(f"\n[→ WRITE CONTENT to {path}]:\n{repr(text)}\n{'─'*50}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return {"status": "ok", "cmd": cmd, "path": path, "message": f"Written to {path}"}

        elif cmd == "delete":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            os.remove(path)
            return {"status": "ok", "cmd": cmd, "path": path, "message": f"Deleted {path}"}

        elif cmd == "addfile":
            path, _ = extract_path_and_args(rest, 0)
            if os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File already exists: {path}"}
            open(path, "x", encoding="utf-8").close()
            return {"status": "ok", "cmd": cmd, "path": path, "message": f"Created {path}"}

        elif cmd == "listfile":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Folder not found: {path}"}
            files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
            return {"status": "ok", "cmd": cmd, "path": path, "files": files}

        elif cmd == "get_folders":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Folder not found: {path}"}
            folders = [f for f in os.listdir(path) if os.path.isdir(os.path.join(path, f))]
            return {"status": "ok", "cmd": cmd, "path": path, "folders": folders}

        elif cmd == "extFiles":
            path, args = extract_path_and_args(rest, 1)
            ext = args[0] if args else ""
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Folder not found: {path}"}
            matched = [f for f in os.listdir(path) if f.endswith(ext)]
            return {"status": "ok", "cmd": cmd, "path": path, "ext": ext, "files": matched}

        elif cmd == "size":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            return {"status": "ok", "cmd": cmd, "path": path, "size": os.path.getsize(path), "unit": "bytes"}

        elif cmd == "editline":
            path, args = extract_path_and_args(rest, 1)
            line_number = int(args[0])
            new_content = content if content is not None else ""
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if line_number < 1 or line_number > len(lines):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Line {line_number} out of range, file has {len(lines)} lines"}
            lines[line_number - 1] = new_content + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return {"status": "ok", "cmd": cmd, "path": path, "line": line_number, "message": f"Line {line_number} updated"}

        elif cmd == "deleteline":
            path, args = extract_path_and_args(rest, 1)
            line_number = int(args[0])
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if line_number < 1 or line_number > len(lines):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Line {line_number} out of range, file has {len(lines)} lines"}
            del lines[line_number - 1]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return {"status": "ok", "cmd": cmd, "path": path, "line": line_number, "message": f"Line {line_number} deleted"}

        elif cmd == "startwithline":
            path, args = extract_path_and_args(rest, 1)
            line_number = int(args[0])
            new_content = content if content is not None else ""
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            new_lines = [l + "\n" for l in new_content.split("\n")]
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            while len(lines) < line_number:
                lines.append("\n")
            for i, new_line in enumerate(new_lines):
                lines.insert(line_number + i, new_line)
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return {"status": "ok", "cmd": cmd, "path": path, "line": line_number, "message": f"Inserted after line {line_number}"}

        elif cmd == "listall":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Folder not found: {path}"}
            items = os.listdir(path)
            files = [f for f in items if os.path.isfile(os.path.join(path, f))]
            folders = [f for f in items if os.path.isdir(os.path.join(path, f))]
            return {
                "status": "ok",
                "cmd": cmd,
                "path": path,
                "files": files,
                "folders": folders
            }
        elif cmd == "mkdir":
            path, _ = extract_path_and_args(rest, 0)
            os.makedirs(path, exist_ok=True)
            return {"status": "ok", "cmd": cmd, "path": path, "message": f"Created {path}"}
        elif cmd == "rmdir":
            path, _ = extract_path_and_args(rest, 0)
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Folder not found: {path}"}
            shutil.rmtree(path)
            return {"status": "ok", "cmd": cmd, "path": path, "message": f"Deleted {path}"}

        elif cmd == "read_from":
            path, args = extract_path_and_args(rest, 1)
            start, end = parse_range(args[0] if args else "")
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)
            if start < 1 or end < start:
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Invalid range {start}-{end}"}
            if start > total:
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Start line {start} out of range, file has {total} lines"}
            eff_end = min(end, total)
            selected = lines[start - 1:eff_end]
            return {"status": "ok", "cmd": cmd, "path": path, "start": start, "end": eff_end,
                    "requested_end": end, "content": "".join(selected)}

        elif cmd == "edit_with-line":
            path, args = extract_path_and_args(rest, 1)
            start, end = parse_range(args[0] if args else "")
            new_content = content if content is not None else ""
            if not os.path.exists(path):
                return {"status": "error", "cmd": cmd, "path": path, "message": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)
            if start < 1 or end < start:
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Invalid range {start}-{end}"}
            if start > total:
                return {"status": "error", "cmd": cmd, "path": path, "message": f"Start line {start} out of range, file has {total} lines"}
            if end > total:
                return {"status": "error", "cmd": cmd, "path": path, "message": f"End line {end} out of range, file has {total} lines"}
            new_lines = [l + "\n" for l in new_content.split("\n")]
            lines[start - 1:end] = new_lines
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return {"status": "ok", "cmd": cmd, "path": path, "start": start, "end": end,
                    "message": f"Lines {start}-{end} replaced"}

    except Exception as e:
        return {"status": "error", "cmd": cmd, "message": str(e)}

def parse_request(raw: str):
    raw = raw.strip()
    if "{" in raw:
        cmd_part, rest = raw.split("{", 1)
        content = rest[:rest.rfind("}")] if "}" in rest else rest
        return cmd_part.strip(), content
    return raw, None

def split_commands(raw: str):
    result = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if "{" in line and "}" not in line:
            block = line
            depth = 1
            i += 1
            while i < len(lines):
                block += "\n" + lines[i]
                depth += lines[i].count("{") - lines[i].count("}")
                if depth <= 0:
                    break
                i += 1
            result.append(block)
        else:
            if line == "}":
                i += 1
                continue
            result.append(line)
        i += 1
    return result


def handle_client(conn, addr, settings):
    if not connection_allowed(addr[0], settings):
        print(f"[!] Rejected {addr} (blocked by network settings)")
        try:
            conn.close()
        except OSError:
            pass
        return
    print(f"[+] Connected: {addr}")
    try:
        marker = b"END"
        while True:
            raw = bytearray()
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                tail_start = len(raw) - (len(marker) - 1)
                raw += chunk
                if tail_start < 0:
                    tail_start = 0
                if marker in raw[tail_start:]:
                    break
            buffer = raw[:raw.rfind(marker)].decode("utf-8", errors="replace").strip()

            clean_lines = []
            for line in buffer.splitlines():
                clean = line.replace("END", "")
                if clean.strip():
                    clean_lines.append(clean)
            buffer = "\n".join(clean_lines)
            print(f"\n[→ RAW BUFFER]:\n{buffer}\n{'─'*50}")
            print(f"\n[→ received] {len(buffer)} chars from {addr}")
            print(f"{'─'*50}")
            print(buffer[:500] + ("..." if len(buffer) > 500 else ""))
            print(f"{'─'*50}")

            cmds = split_commands(buffer)
            print(f"[→ commands] {len(cmds)} command(s) detected")
            for i, c in enumerate(cmds):
                print(f"  [{i+1}] {c[:100]}")

            results = []
            for raw_cmd in cmds:
                cmd, content = parse_request(raw_cmd)
                print(f"\n[→ executing] {cmd}" + (f" | content: {content[:50]}..." if content and len(content) > 50 else f" | content: {content}" if content else ""))
                result = runner(cmd, content=content)
                print(f"[← result] {json.dumps(result, ensure_ascii=False)[:200]}")
                results.append(result)

            response = json.dumps({"results": results}, ensure_ascii=False, indent=2)
            print(f"\n[← sending] {len(response)} chars to {addr}")
            conn.sendall((response + "\n<<EOF>>\n").encode("utf-8"))
            print(f"[← sent] <<EOF>>")

    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        print(f"[-] Connection lost: {addr}")
    except Exception as e:
        print(f"[!] Error from {addr}: {e}")
        err = json.dumps({"results": [{"status": "error", "message": str(e)}]}, ensure_ascii=False)
        try:
            conn.sendall((err + "\n<<EOF>>\n").encode("utf-8"))
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[-] Disconnected: {addr}")


def start_server():
    sync_blocks()
    settings = load_settings()
    port = int(settings.get("port", DEFAULT_PORT))
    public = bool(settings.get("public"))
    private = bool(settings.get("private"))
    host = "0.0.0.0" if (public or private) else "127.0.0.1"
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    print(f"[*] FileAPI listening on {host}:{port} (public={public}, private={private})")
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr, settings), daemon=True).start()


if __name__ == "__main__":
    start_server()
