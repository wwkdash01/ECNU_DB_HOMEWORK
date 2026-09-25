"""__main__.py —— `python -m db_agent <子命令>` 的入口。

只做三件事：解析子命令 → 改写 sys.argv → 用 runpy 以 `__main__` 执行目标模块。
**不 import 目标模块**，因此各脚本原有的 `__main__` 块逐字节原样执行 ——
这是"统一入口但不改任何实验逻辑"的前提。
"""
import runpy
import sys

from db_agent.cli import HELP, resolve


def main():
    argv = sys.argv[1:]
    module, tail = resolve(argv)
    if module is None:
        print(HELP, end="")
        return 0
    # argv[0] 换成可读名字，方便各脚本的 argparse 报错信息里显示正确的程序名
    sys.argv = [f"db_agent {argv[0]}", *tail]
    runpy.run_module(module, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
