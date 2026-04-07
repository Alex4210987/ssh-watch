#!/usr/bin/env python3
"""
Enumerate Host entries from OpenSSH client config and probe each with a non-interactive SSH.
Batch mode prints once; --top is a full-screen live dashboard (like top).
"""

from __future__ import annotations

import argparse
import curses
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.2.0"


def expand_path(p: str, base_dir: Path | None = None) -> Path:
    expanded = os.path.expanduser(p)
    path = Path(expanded)
    if not path.is_absolute() and base_dir is not None:
        path = (base_dir / path).resolve()
    return path


def _is_probeable_host(name: str) -> bool:
    if not name or name.strip() != name:
        return False
    if name == "*":
        return False
    for ch in "*?!":
        if ch in name:
            return False
    return True


_INCLUDE_RE = re.compile(r"^\s*Include\s+(.+?)\s*$", re.IGNORECASE)


def _parse_include_line(line: str) -> list[str]:
    m = _INCLUDE_RE.match(line)
    if not m:
        return []
    rest = m.group(1).strip()
    if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
        rest = rest[1:-1]
    return rest.split()


def collect_hosts_from_file(path: Path, seen_files: set[Path] | None = None) -> set[str]:
    if seen_files is None:
        seen_files = set()
    path = path.resolve()
    if path in seen_files or not path.is_file():
        return set()
    seen_files.add(path)
    hosts: set[str] = set()
    base_dir = path.parent
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hosts
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        inc_parts = _parse_include_line(raw)
        if inc_parts:
            for part in inc_parts:
                glob_path = expand_path(part.strip(), base_dir)
                if any(c in str(glob_path) for c in "*?["):
                    for p in glob_path.parent.glob(glob_path.name):
                        hosts |= collect_hosts_from_file(p, seen_files)
                else:
                    hosts |= collect_hosts_from_file(glob_path, seen_files)
            continue
        if line.lower().startswith("host "):
            names = line[5:].split()
            for n in names:
                if _is_probeable_host(n):
                    hosts.add(n)
    return hosts


def default_config_path() -> Path:
    return Path(os.path.expanduser("~/.ssh/config"))


def probe_host(
    host: str,
    timeout: int,
    connect_timeout: int,
    remote_cmd: str,
) -> tuple[str, bool, float | None, str]:
    """
    Returns (host, ok, seconds_elapsed_or_None, message).
    """
    ssh = os.environ.get("SSH", "ssh")
    args = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=" + str(connect_timeout),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "NumberOfPasswordPrompts=0",
        host,
        remote_cmd,
    ]
    start = time.perf_counter()
    try:
        r = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - start
        if r.returncode == 0:
            return host, True, elapsed, "ok"
        err = (r.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"exit {r.returncode}"
        return host, False, elapsed, msg
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        return host, False, elapsed, f"timeout ({timeout}s)"
    except FileNotFoundError:
        return host, False, None, "ssh binary not found"
    except OSError as e:
        return host, False, None, str(e)


def run_probe_round(
    hosts: list[str],
    jobs: int,
    timeout: int,
    connect_timeout: int,
    remote_cmd: str,
) -> dict[str, tuple[bool, float | None, str]]:
    jobs = max(1, jobs)
    out: dict[str, tuple[bool, float | None, str]] = {}
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {
            ex.submit(probe_host, h, timeout, connect_timeout, remote_cmd): h for h in hosts
        }
        for fut in as_completed(futs):
            host, ok, elapsed, msg = fut.result()
            lat_ms = elapsed * 1000.0 if elapsed is not None else None
            out[host] = (ok, lat_ms, msg)
    return out


SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(samples: deque[tuple[bool, float | None]], width: int) -> list[tuple[str, bool]]:
    """
    Returns list of (char, ok) for colored drawing.
    """
    if width <= 0:
        return []
    recent = list(samples)[-width:]
    if not recent:
        return [("·", False)] * width
    lats = [lat for ok, lat in recent if ok and lat is not None]
    if lats:
        lo = min(lats)
        hi = max(lats)
        if hi <= lo:
            hi = lo + 1.0
    else:
        lo, hi = 0.0, 1.0
    out: list[tuple[str, bool]] = []
    for ok, lat in recent:
        if not ok or lat is None:
            out.append(("×", False))
        else:
            t = (lat - lo) / (hi - lo)
            idx = max(0, min(7, int(t * 7.999)))
            out.append((SPARK_CHARS[idx], True))
    while len(out) < width:
        out.append(("·", False))
    return out[:width]


@dataclass
class HostRow:
    ok: bool = False
    lat_ms: float | None = None
    msg: str = ""
    history: deque[tuple[bool, float | None]] = field(default_factory=lambda: deque(maxlen=48))
    round_tag: int = 0
    fail_streak: int = 0
    state_since_ts: float | None = None


def _as_str(s: str) -> str:
    """Wrap a string in AppleScript double-quoted literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send_macos_notification(
    title: str,
    message: str,
    subtitle: str = "ssh-watch",
    debug: bool = False,
) -> bool:
    """
    Send a macOS notification.
    Tries terminal-notifier first (more reliable on Sequoia+), then falls
    back to osascript display notification.
    """
    import shutil

    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [
            tn,
            "-title", title,
            "-subtitle", subtitle,
            "-message", message,
            "-sound", "default",
        ]
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if debug else subprocess.DEVNULL,
                stderr=subprocess.PIPE if debug else subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=5,
            )
            if debug and res.returncode != 0:
                print(f"[notify] terminal-notifier failed (rc={res.returncode}): {(res.stderr or '').strip()}", file=sys.stderr)
            return res.returncode == 0
        except OSError:
            if debug:
                print("[notify] terminal-notifier exec failed, falling back", file=sys.stderr)

    # Fallback: osascript
    script = (
        f"display notification {_as_str(message)} "
        f"with title {_as_str(title)} "
        f"subtitle {_as_str(subtitle)}"
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if debug else subprocess.DEVNULL,
            stderr=subprocess.PIPE if debug else subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=3,
        )
        if debug and res.returncode != 0:
            print(f"[notify] osascript failed: {(res.stderr or '').strip()}", file=sys.stderr)
        return res.returncode == 0
    except OSError:
        if debug:
            print("[notify] osascript not available", file=sys.stderr)
        return False


def format_duration_short(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    mins, sec = divmod(total, 60)
    if mins < 60:
        return f"{mins}m{sec:02d}s"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h{mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def run_top_ui(stdscr: curses.window, hosts: list[str], args: argparse.Namespace) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(80)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        # pair 1: up (green), 2: down (red), 3: header (cyan), 4: dim note
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)

    A_HEAD = curses.color_pair(3) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
    A_DIM = curses.color_pair(4) if curses.has_colors() else curses.A_DIM
    A_S_UP = curses.color_pair(1) if curses.has_colors() else 0
    A_S_DOWN = curses.color_pair(2) if curses.has_colors() else 0

    rows: dict[str, HostRow] = {h: HostRow() for h in hosts}
    result_q: queue.Queue[dict[str, tuple[bool, float | None, str]]] = queue.Queue()
    stop_ev = threading.Event()
    refresh_ev = threading.Event()
    sort_fail_first = True
    round_id = 0
    last_summary = (0, 0, 0.0)  # ok, fail, t_done
    notify_count = 0
    notify_ok = False
    if args.notify:
        notify_ok = send_macos_notification(
            "ssh-watch started",
            f"Monitoring {len(hosts)} hosts (threshold={args.notify_fail_streak})",
            debug=args.notify_debug,
        )

    def one_round() -> None:
        nonlocal round_id, last_summary
        round_id += 1
        t0 = time.perf_counter()
        data = run_probe_round(
            hosts,
            args.jobs,
            args.timeout,
            args.connect_timeout,
            args.command,
        )
        dt = time.perf_counter() - t0
        ok_c = sum(1 for v in data.values() if v[0])
        last_summary = (ok_c, len(data) - ok_c, dt)
        result_q.put(data)

    def worker() -> None:
        one_round()
        while not stop_ev.is_set():
            end = time.time() + max(0.5, args.interval)
            while time.time() < end:
                if stop_ev.is_set():
                    return
                if refresh_ev.is_set():
                    refresh_ev.clear()
                    break
                time.sleep(0.05)
            if stop_ev.is_set():
                return
            one_round()

    th = threading.Thread(target=worker, name="ssh-watch-probe", daemon=True)
    th.start()

    scroll = 0

    def sorted_hosts() -> list[str]:
        hs = list(hosts)
        if sort_fail_first:

            def key(h: str) -> tuple[int, str]:
                r = rows[h]
                return (0 if not r.ok else 1, h)

            hs.sort(key=key)
        else:
            hs.sort()
        return hs

    try:
        while True:
            while True:
                try:
                    data = result_q.get_nowait()
                except queue.Empty:
                    break
                for h, (ok, lat_ms, msg) in data.items():
                    row = rows[h]
                    now_ts = time.time()
                    prev_ok = row.ok
                    had_prev = row.round_tag > 0
                    row.ok = ok
                    row.lat_ms = lat_ms
                    row.msg = msg
                    row.round_tag = round_id
                    row.history.append((ok, lat_ms))
                    if (not had_prev) or (ok != prev_ok):
                        row.state_since_ts = now_ts
                    if ok:
                        if row.fail_streak >= args.notify_fail_streak and args.notify:
                            sent = send_macos_notification(
                                "SSH recovered",
                                f"{h} is reachable again",
                                debug=args.notify_debug,
                            )
                            if sent:
                                notify_count += 1
                        row.fail_streak = 0
                    else:
                        row.fail_streak += 1
                        if (
                            args.notify
                            and args.notify_fail_streak > 0
                            and row.fail_streak % args.notify_fail_streak == 0
                        ):
                            sent = send_macos_notification(
                                "SSH unreachable",
                                f"{h} failed {row.fail_streak} times in a row",
                                debug=args.notify_debug,
                            )
                            if sent:
                                notify_count += 1
            h_max, w_max = stdscr.getmaxyx()
            stdscr.erase()

            title = " ssh-watch "
            ok_c, _fail_c, probe_dt = last_summary
            bar_total = len(hosts)
            bar_ok = ok_c
            bar_w = max(10, w_max - len(title) - 42)
            filled = 0
            if bar_total > 0 and bar_w > 0:
                filled = int(bar_ok * bar_w / bar_total)
            bar_vis = "█" * filled + "░" * (bar_w - filled)
            now_s = time.strftime("%H:%M:%S")
            head = (
                f"{title}│ UP {ok_c}/{bar_total} "
                f"{bar_vis} "
                f"│ {now_s} │ last {probe_dt:.1f}s"
            )
            try:
                stdscr.addstr(0, 0, head[: w_max - 1], A_HEAD)
            except curses.error:
                pass

            sub = (
                " [q]uit  [r]efresh  [s]ort  [↑↓]scroll  "
                f"int {args.interval:g}s  │  history ▁▂…█ = latency   × = fail"
            )
            try:
                stdscr.addstr(1, 0, sub[: w_max - 1], A_DIM)
            except curses.error:
                pass

            col_host = min(28, max(12, w_max // 4))
            col_st = 4
            col_ms = 7
            col_for = 8
            spark_w = max(8, min(24, w_max - col_host - col_st - col_ms - col_for - 6))
            hdr = (
                f"{'HOST':<{col_host}} {'ST':<{col_st}} {'MS':>{col_ms}} "
                f"{'FOR':>{col_for}}  {'HISTORY':<{spark_w}}"
            )
            try:
                stdscr.addstr(2, 0, hdr[: w_max - 1], A_HEAD)
            except curses.error:
                pass

            y_data = 3
            avail = max(0, h_max - y_data - 1)
            ordered = sorted_hosts()
            total_rows = len(ordered)
            scroll = max(0, min(scroll, max(0, total_rows - avail)))

            for i, host in enumerate(ordered[scroll : scroll + avail]):
                y = y_data + i
                if y >= h_max - 1:
                    break
                r = rows[host]
                st = "UP" if r.ok else "DN"
                ms = f"{r.lat_ms:.0f}" if r.lat_ms is not None else "-"
                if len(ms) > col_ms:
                    ms = ms[: col_ms]
                dur = "-"
                if r.state_since_ts is not None:
                    dur = format_duration_short(time.time() - r.state_since_ts)
                host_vis = host if len(host) <= col_host else host[: col_host - 1] + "…"
                line_start = f"{host_vis:<{col_host}} {st:<{col_st}} {ms:>{col_ms}} {dur:>{col_for}}  "
                try:
                    stdscr.addstr(y, 0, line_start[: w_max - 1])
                except curses.error:
                    continue
                x_spark = len(line_start)
                sl = sparkline(r.history, spark_w)
                for j, (ch, up) in enumerate(sl):
                    if x_spark + j >= w_max - 1:
                        break
                    try:
                        stdscr.addstr(y, x_spark + j, ch, A_S_UP if up else A_S_DOWN)
                    except curses.error:
                        pass
                msg = r.msg.replace("\n", " ")
                x_msg = x_spark + spark_w + 1
                if x_msg < w_max - 2:
                    rest = w_max - 1 - x_msg
                    if rest > 1:
                        try:
                            stdscr.addstr(y, x_msg, msg[:rest], A_DIM)
                        except curses.error:
                            pass

            foot = (
                f" {scroll + 1}-{min(scroll + avail, total_rows)}/{total_rows} hosts"
                f"  │  fail-first: {sort_fail_first}"
                f"  │  notify: {args.notify}({notify_ok}) sent={notify_count}"
            )
            try:
                stdscr.addstr(h_max - 1, 0, foot[: w_max - 1], A_DIM)
            except curses.error:
                pass

            stdscr.refresh()
            ch = stdscr.getch()
            if ch == ord("q") or ch == ord("Q"):
                break
            if ch == ord("r") or ch == ord("R"):
                refresh_ev.set()
            if ch == ord("s") or ch == ord("S"):
                sort_fail_first = not sort_fail_first
            if ch == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            if ch == curses.KEY_DOWN:
                scroll = min(max(0, total_rows - avail), scroll + 1)
            if ch == curses.KEY_RESIZE:
                pass
            if ch == curses.KEY_HOME:
                scroll = 0
            if ch == curses.KEY_END:
                scroll = max(0, total_rows - avail)
    finally:
        stop_ev.set()
        th.join(timeout=max(args.timeout, args.connect_timeout) + 5)


def run_batch(args: argparse.Namespace, hosts: list[str]) -> int:
    jobs = max(1, args.jobs)
    results: list[tuple[str, bool, float | None, str]] = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {
            ex.submit(
                probe_host,
                h,
                args.timeout,
                args.connect_timeout,
                args.command,
            ): h
            for h in hosts
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    results.sort(key=lambda x: x[0])
    ok_n = sum(1 for _, ok, _, _ in results if ok)
    fail_n = len(results) - ok_n

    w_host = max(len("HOST"), max(len(h) for h, _, _, _ in results))
    for host, ok, elapsed, msg in results:
        if args.quiet and ok:
            continue
        lat = f"{elapsed * 1000:.0f}ms" if elapsed is not None else "-"
        status = "UP" if ok else "DOWN"
        print(f"{host:<{w_host}}  {status:4}  {lat:>6}  {msg}")

    print(f"\n{ok_n} ok, {fail_n} failed (of {len(results)})", file=sys.stderr)
    return 1 if fail_n else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check SSH config hosts with a quick non-interactive ssh run.",
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    ap.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="SSH config file (default: ~/.ssh/config)",
    )
    ap.add_argument(
        "--hosts",
        nargs="*",
        metavar="NAME",
        help="Only these host aliases (default: all literal Host names from config)",
    )
    ap.add_argument(
        "--command",
        default="true",
        help='Remote command to run (default: "true")',
    )
    ap.add_argument(
        "--connect-timeout",
        type=int,
        default=5,
        metavar="SEC",
        help="SSH ConnectTimeout per host (default: 5)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=25,
        metavar="SEC",
        help="Overall subprocess timeout per host (default: 25)",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=12,
        help="Parallel probes (default: 12)",
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print failures",
    )
    ap.add_argument(
        "--top",
        action="store_true",
        help="Full-screen live view (arrow keys scroll, q quit)",
    )
    ap.add_argument(
        "-i",
        "--interval",
        type=float,
        default=8.0,
        metavar="SEC",
        help="Seconds between probe rounds in --top (default: 8)",
    )
    ap.add_argument(
        "--notify",
        action="store_true",
        help="macOS notification in --top mode when fail streak threshold is crossed and on recovery",
    )
    ap.add_argument(
        "--notify-fail-streak",
        type=int,
        default=10,
        metavar="N",
        help="Consecutive failures required before down notification (default: 10)",
    )
    ap.add_argument(
        "--notify-debug",
        action="store_true",
        help="Print notification command errors to stderr (for troubleshooting)",
    )
    args = ap.parse_args()
    cfg = args.config or default_config_path()
    if args.hosts:
        host_list = sorted(set(args.hosts))
    else:
        if not cfg.is_file():
            print(f"No config file: {cfg}", file=sys.stderr)
            return 2
        host_list = sorted(collect_hosts_from_file(cfg))
    if not host_list:
        print("No hosts to check.", file=sys.stderr)
        return 2

    if args.top:
        try:
            curses.wrapper(lambda scr: run_top_ui(scr, host_list, args))
        except curses.error as e:
            print(f"Terminal UI failed: {e}", file=sys.stderr)
            return 2
        return 0

    return run_batch(args, host_list)


if __name__ == "__main__":
    raise SystemExit(main())
