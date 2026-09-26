import runpy
import sys

from db_agent.cli import HELP, resolve


def main():
    argv = sys.argv[1:]
    module, tail = resolve(argv)
    if module is None:
        print(HELP, end="")
        return 0
    sys.argv = [f"db_agent {argv[0]}", *tail]
    runpy.run_module(module, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
