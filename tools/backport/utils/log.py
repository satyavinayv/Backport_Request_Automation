import sys


def err(msg):
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"[WARN]  {msg}")


def info(msg):
    print(f"[INFO]  {msg}")
