# ssh-watch

A terminal tool for monitoring whether SSH servers in your `~/.ssh/config` can be logged into — with a live full-screen dashboard (like `top`) and per-host latency sparklines.

> Terminal dashboard (sample):

```text
 ssh-watch │ UP 3/8 ████████░░░░░░ │ 14:23:01 │ last 5.2s
 [q]uit  [r]efresh  [s]ort  [↑↓]scroll  int 8s  │  history ▁▂…█ = latency   × = fail
 HOST                         ST      MS  HISTORY
 devbox                       UP     312  ▃▃▄▃▄▄▃▂▃▃▅▄▃▄▄▄▂▃▂▃▃▄▃▃▄▂▃
 work-gpu                     UP     892  ▅▄▆▅▅▄▆▅▅▄▅▄▄▄▅▅▄▃▄▅▄▆▅▄▅▄▃
 myvm                         UP     145  ▁▁▂▁▂▁▁▂▁▂▁▁▂▂▁▂▁▂▁▁▂▁▁▁▂▁▁
 cancon.hpccube.com           DN     863  ××××××××××××                 Permission denied (publickey)
 162.105.146.175              DN    5042  ××××××××××××                 Connection timed out
```

## Features

- Reads host aliases from `~/.ssh/config` automatically (follows `Include` directives)
- Live full-screen dashboard (`--top`) with color: **green** = reachable, **red** = down
- Sparkline history column: block height ∝ latency, `×` = failed probe
- Parallel probing — checks dozens of hosts in seconds
- Batch mode (no `--top`) for scripts / cron use; non-zero exit if any host is down
- Zero dependencies — pure Python 3.8+ stdlib

## Requirements

- Python ≥ 3.8
- `ssh` in your `$PATH`
- Hosts configured with key-based (non-interactive) authentication

> **Note:** Probes use `BatchMode=yes` — hosts requiring a password will show as **DOWN**.
> This is intentional; use `ssh-agent` or `authorized_keys` for passwordless access.

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/ssh-watch.git
cd ssh-watch
chmod +x ssh_watch.py
```

Optionally symlink to your PATH:

```bash
ln -s "$PWD/ssh_watch.py" /usr/local/bin/ssh-watch
```

## Usage

### Live dashboard (recommended)

```bash
python3 ssh_watch.py --top
```

### Key bindings in dashboard

| Key | Action |
| --- | --- |
| `q` | Quit |
| `r` | Refresh immediately (skip interval wait) |
| `s` | Toggle sort: fail-first ↔ alphabetical |
| `↑` / `↓` | Scroll host list |
| `Home` / `End` | Jump to top / bottom |

### macOS notifications

Add `--notify` to get native macOS banners automatically:

- **Down alert**: fires once when a host fails **10 consecutive probes** in a row
- **Recovery alert**: fires once when that host comes back up

```bash
python3 ssh_watch.py --top --notify
```

Raise or lower the threshold with `--notify-fail-streak N` (default: 10):

```bash
# Alert after 3 consecutive failures
python3 ssh_watch.py --top --notify --notify-fail-streak 3
```

> Notifications use `osascript` and require macOS notification permissions for Terminal / iTerm.

### Batch mode (single pass, for scripts)

```bash
python3 ssh_watch.py              # check all hosts, print table
python3 ssh_watch.py -q           # only print failures
```

Exit code is `0` if all hosts are up, `1` if any are down.

### Common options

| Flag | Default | Description |
| --- | --- | --- |
| `-c FILE` | `~/.ssh/config` | Use a different SSH config file |
| `--hosts A B …` | *(all)* | Only check these aliases |
| `-i SEC` | `8` | Probe interval in `--top` mode |
| `-j N` | `12` | Parallel probes |
| `--connect-timeout SEC` | `5` | SSH `ConnectTimeout` |
| `--timeout SEC` | `25` | Hard subprocess timeout |
| `--command CMD` | `true` | Remote command to run (default is instant) |
| `--notify` | off | Enable macOS notifications |
| `--notify-fail-streak N` | `10` | Consecutive fails before down alert |

### Examples

```bash
# Only monitor a subset of hosts every 15 seconds
python3 ssh_watch.py --top --hosts myvm work devbox -i 15

# High concurrency for large inventories
python3 ssh_watch.py --top -j 40 --connect-timeout 3

# Cron-friendly: alert on any failure
python3 ssh_watch.py -q && echo "all up" || echo "some hosts down"

# Use a non-default config (e.g. a project-specific one)
python3 ssh_watch.py -c ~/projects/infra/.ssh/config --top
```

## How it works

1. Parses `~/.ssh/config` to collect all **literal** `Host` aliases (wildcard patterns like `Host *.internal` are skipped — they can't be dialed directly).
2. For each host, spawns:

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=N -o NumberOfPasswordPrompts=0 <host> true
   ```

3. **Exit code 0** → host is UP; anything else → DOWN.
4. In `--top` mode, a background thread runs probe rounds in a loop; the curses UI refreshes independently at ~12 fps.

## License

MIT
