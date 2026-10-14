"""Remote instances: the teams run on another machine, this one drives them over ssh.

The target is `--remote host:path`, else `$TEAMS_REMOTE`, else the nearest `.teams/remote.json`
or `teams/remote.json` walking up from the cwd (a `teams.json` found nearer keeps the instance
local). `init` and `link` only go remote with `--remote`; agents on the server have `TEAMS_CONFIG`
set and never go remote. A command with a target runs over there as `teams <same arguments>`,
inside the project, through the remote user's login shell so its PATH finds teams and claude.
Every word reaches that shell between single quotes: none is left bare for it to expand.

remote.json: {"host": "sky-a", "path": "~/work/project"}, optionally "command" (default "teams")
and "ssh_options" (a list of ssh arguments). It belongs to one machine: keep it out of git.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

CANDIDATES = (".teams/remote.json", "teams/remote.json")


class RemoteError(OSError):
    """The server could not be reached, or did not answer as expected."""


def parse_target(spec: str) -> dict:
    host, sep, path = spec.partition(":")
    if not sep or not host or not path:
        raise ValueError(f"remote target must be host:path, got {spec!r}")
    return {"host": host, "path": path}


def find(cwd: Path, local_candidates) -> dict | None:
    """$TEAMS_REMOTE, else the nearest remote.json walking up; None when a local teams.json is nearer."""
    if os.environ.get("TEAMS_REMOTE"):
        return parse_target(os.environ["TEAMS_REMOTE"])
    for base in [cwd, *cwd.parents]:
        for rel in CANDIDATES:
            f = base / rel
            if f.is_file():
                target = json.loads(f.read_text())
                if not target.get("host") or not target.get("path"):
                    raise ValueError(f"{f}: needs \"host\" and \"path\"")
                return {**target, "_file": f}
        if any((base / rel).is_file() for rel in local_candidates):
            return None
    return None


def quote(word: str) -> str:
    """A word as the server's shell must read it: always between single quotes, never bare.
    A bare word is the shell's to expand, and which words it expands depends on the shell: zsh
    turns `=ls` into `/bin/ls`, which `shlex.quote` leaves bare."""
    return "'" + word.replace("'", "'\"'\"'") + "'"


def _remote_path(path: str) -> str:
    """The project path for the server's shell, leaving a leading ~ outside the quotes for it to expand."""
    if path == "~" or path.startswith("~/"):
        rest = path[2:]
        return "~/" + quote(rest) if rest else "~"
    return quote("./" + path if path.startswith("-") else path)    # never an option of cd


def remote_line(target: dict, argv: list) -> str:
    """`teams <argv>` in the project, through a login shell so the server's PATH finds teams and claude."""
    inner = f"cd {_remote_path(target['path'])} && {target.get('command', 'teams')} {' '.join(quote(x) for x in argv)}"
    return f'exec "$SHELL" -lc {quote(inner)}'


def ssh_argv(target: dict, line: str | None = None, tty: bool = False, options=()) -> list:
    return ["ssh", *(["-t"] if tty else []), *options, *target.get("ssh_options", []), target["host"],
            *([line] if line else [])]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(target: dict, argv: list) -> int:
    """`teams <argv>` on the server, its output streamed here; its exit code."""
    try:
        return subprocess.call(ssh_argv(target, remote_line(target, argv)))
    except OSError as exc:
        raise RemoteError(f"ssh {target['host']}: {exc}")


def relay(target: dict, argv: list):
    """Hand this process over to ssh: exit code, signals and terminal behave as if the command ran here."""
    os.execvp("ssh", ssh_argv(target, remote_line(target, argv), tty=_interactive()))


def status(target: dict) -> dict:
    """`teams status --json` on the server (its last JSON line: a login shell may print before it)."""
    try:
        r = subprocess.run(ssh_argv(target, remote_line(target, ["status", "--json"])),
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteError(f"ssh {target['host']}: {exc}")
    line = next((l for l in reversed(r.stdout.split("\n")) if l.startswith("{")), None)
    if r.returncode != 0 or not line:
        why = (r.stderr.strip().splitlines() or [f"exit {r.returncode}, no JSON on stdout"])[-1]
        if "--json" in why:
            why += " (the engine over there predates remote instances: update it with git pull)"
        raise RemoteError(f"{target['host']}:{target['path']}: {why}")
    return json.loads(line)


def link(spec: str, inst_dir: str) -> int:
    """Point a local directory at a remote instance, once the server has answered."""
    target = parse_target(spec)
    st = status(target)
    f = Path(inst_dir).resolve() / "remote.json"
    try:
        old = json.loads(f.read_text())
        previous = f"{old.get('host')}:{old.get('path')}"
    except (OSError, json.JSONDecodeError):
        previous = None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(target, indent=2) + "\n")
    names = ", ".join(t["name"] for t in st.get("teams", []))
    print(f"linked {f} → {spec}" + (f" (was {previous})" if previous and previous != spec else ""))
    print(f"  teams: {names or '(none)'} — {'up' if st.get('up') else 'down'}, engine {st.get('engine', '?')}")
    print(f"next: `teams up` from anywhere under {f.parent.parent}")
    return 0


def strip_option(argv: list, name: str) -> list:
    """argv without `name value` / `name=value`: the server finds its own instance.

    Only the options ahead of the command are read: from the command (or a `--`) on, the words are
    the command's own and travel untouched. `name` must be spelled in full, which the launcher's
    parser enforces by refusing abbreviations.
    """
    out, i = [], 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i] == name:
            i += 2
            continue
        if not argv[i].startswith(name + "="):
            out.append(argv[i])
        i += 1
    return out + argv[i:]


def main(target: dict, a, argv: list):
    """Run a command whose instance lives on another machine."""
    relay(target, argv)
