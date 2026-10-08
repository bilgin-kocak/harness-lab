"""Stand-in for the codex / claude executables used in adapter tests.

Behaviour is controlled by environment variables (set by the tests):

    FAKE_CLI_STREAM   path to a JSONL file replayed to stdout line by line
    FAKE_CLI_EXIT     exit code (default 0)
    FAKE_CLI_MODE     replay (default) | hang | bigline | noresult
    FAKE_CLI_TOUCH    optional path of a file to create inside the cwd (simulated edit)
    FAKE_CLI_VERSION  text printed for --version
    FAKE_CLI_HELP     path to a help text printed for --help (nothing is printed when unset)
    FAKE_CLI_HELP_RESUME  help text for ``exec resume --help`` (falls back to FAKE_CLI_HELP)
    FAKE_CLI_REJECT   a flag the CLI rejects at startup, the way a CLI that dropped it would
    FAKE_CLI_HELP_LOG path of a file that gets one line per --help call
    FAKE_CLI_STDERR_TEXT  text written to stderr at the end of a normal run

The prompt is read from stdin and echoed to FAKE_CLI_PROMPT_OUT when set.
"""

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(os.environ.get("FAKE_CLI_VERSION", "fake-cli 9.9.9"))
        return 0
    if "--help" in argv:
        log = os.environ.get("FAKE_CLI_HELP_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("help\n")
        help_file = os.environ.get("FAKE_CLI_HELP")
        if "resume" in argv:
            help_file = os.environ.get("FAKE_CLI_HELP_RESUME") or help_file
        if help_file:
            with open(help_file, encoding="utf-8") as fh:
                sys.stdout.write(fh.read())
        return 0
    reject = os.environ.get("FAKE_CLI_REJECT")
    if reject and reject in argv:
        if "exec" in argv:  # codex (clap) wording
            sys.stderr.write(f"error: unexpected argument '{reject}' found\n")
            return 2
        sys.stderr.write(f"error: unknown option '{reject}'\n")  # claude (commander) wording
        return 1
    mode = os.environ.get("FAKE_CLI_MODE", "replay")
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    out = os.environ.get("FAKE_CLI_PROMPT_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    cwd_out = os.environ.get("FAKE_CLI_CWD_OUT")
    if cwd_out:
        with open(cwd_out, "w", encoding="utf-8") as fh:
            fh.write(os.getcwd())
    touch = os.environ.get("FAKE_CLI_TOUCH")
    if touch:
        with open(touch, "a", encoding="utf-8") as fh:
            fh.write("# touched by fake cli\n")
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        sys.stdout.write('{"type":"turn.started"}\n')
        sys.stdout.flush()
        child.wait()
        return 0
    if mode == "bigline":
        sys.stdout.write(
            '{"type":"item.completed","item":{"id":"big","type":"agent_message","text":"'
            + ("x" * 3_000_000)
            + '"}}\n'
        )
        sys.stdout.flush()
        return 0
    stream = os.environ.get("FAKE_CLI_STREAM")
    if stream and mode in ("replay", "noresult"):
        with open(stream, encoding="utf-8") as fh:
            for line in fh:
                if mode == "noresult" and '"type": "result"' in line.replace(
                    '"type":"result"', '"type": "result"'
                ):
                    continue
                sys.stdout.write(line)
                sys.stdout.flush()
                time.sleep(0.001)
    # Honour codex's -o <file> for the last message.
    if "-o" in argv:
        path = argv[argv.index("-o") + 1]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("final message from -o file\n")
    sys.stderr.write("fake cli finished\n")
    if os.environ.get("FAKE_CLI_STDERR_TEXT"):
        sys.stderr.write(os.environ["FAKE_CLI_STDERR_TEXT"] + "\n")
    if os.environ.get("FAKE_CLI_STDERR_SECRET"):
        sys.stderr.write("leaked token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab\n")
    return int(os.environ.get("FAKE_CLI_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
