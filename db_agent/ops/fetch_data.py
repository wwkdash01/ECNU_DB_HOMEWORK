#!/usr/bin/env python3
"""fetch_data.py —— BIRD Mini-Dev (SQLite) 数据集：下载 → 解压 → 摆正 → 清理

    python -m db_agent fetch                  # data/dev_databases 不完整就下载并摆正
    python -m db_agent fetch --keep-zip       # 保留下载的 zip
    python -m db_agent fetch --force          # 目标已完整时也重建
    python -m db_agent fetch --redownload     # 忽略本地已有 zip，从头下载

本文件实测过的四个前提（写法全部围着它们转）：

  1. zip 内部【只有一个顶层目录 minidev/】，SQLite 版在 minidev/MINIDEV/ 下。
     解压后比预期多一层，不能直接摊到 data/ —— config.DB_DIR 写死要求
     dev_databases 直接躺在 data/ 下。
  2. 只解 minidev/MINIDEV/dev_databases/** 与 3 个 SQLite 小文件：
     minidev/MINIDEV_mysql/BIRD_dev.sql (995 M) 与
     minidev/MINIDEV_postgresql/BIRD_dev.sql (955 M) 本项目完全不用，
     选择性解压让这 1.95 GB【根本不落盘】。
  3. 先解到 data/.fetch_tmp/ 再 os.rename 到 data/dev_databases：
     同卷重命名是瞬时操作，目标目录不会出现半成品，也不额外占 1.4 G。
  4. 删除白名单：只删 data/.fetch_tmp、data/dev_databases 下的 .DS_Store、
     以及本脚本使用的 zip。不做任何通配删除。
"""
import argparse
import hashlib
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    sys.exit("缺少依赖 tqdm。请先执行：\n"
             "  ~/.conda/envs/db_agent/bin/python -m pip install tqdm")

from db_agent import config          # 复用 ROOT / DATA_DIR / DB_DIR，避免路径口径分裂

URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
DEFAULT_ZIP = Path.home() / "Downloads" / "minidev.zip"
EXPECTED_BYTES = 800_943_648            # 实测 764 MiB（用于下载校验 + 本地复用判定）
ZIP_PREFIX = "minidev/MINIDEV/"         # zip 内 SQLite 版的路径前缀
TMP_NAME = ".fetch_tmp"
CHUNK = 1 << 20                         # 1 MiB
DB_COUNT = 11                           # Mini-Dev V1：11 库

# data/ 下必须齐全的 4 个小文件（.jsonl 不在 zip 内，由仓库版本化提供）
REQUIRED_SMALL = ("mini_dev_sqlite.json", "mini_dev_sqlite_gold.sql",
                  "mini_dev_sqlite.jsonl", "dev_tables.json")
# 其中能从 zip 里取到的 3 个
FROM_ZIP_SMALL = ("mini_dev_sqlite.json", "mini_dev_sqlite_gold.sql",
                  "dev_tables.json")


def mib(n):
    return f"{n / 1024 / 1024:.1f} MiB"


# ---------------------------------------------------------------- 目标状态
def target_state():
    """返回 (是否完整, 库目录列表, sqlite 列表, 缺失的小文件)。"""
    db = config.DB_DIR
    if db.exists():
        dirs = sorted(d.name for d in db.iterdir() if d.is_dir())
        sqlites = sorted(db.glob("*/*.sqlite"))
    else:
        dirs, sqlites = [], []
    missing = [f for f in REQUIRED_SMALL if not (config.DATA_DIR / f).exists()]
    ok = (len(dirs) == DB_COUNT and len(sqlites) == DB_COUNT and not missing)
    return ok, dirs, sqlites, missing


def report_state():
    ok, dirs, sqlites, missing = target_state()
    print(f"  目标目录 : {config.DB_DIR}")
    print(f"  库目录   : {len(dirs)}/{DB_COUNT}")
    print(f"  sqlite   : {len(sqlites)}/{DB_COUNT}")
    print(f"  小文件   : {len(REQUIRED_SMALL) - len(missing)}/{len(REQUIRED_SMALL)}"
          + (f"   缺 {missing}" if missing else ""))
    return ok


# ---------------------------------------------------------------- 下载
def download(url, dest, expected, redownload=False):
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if redownload and dest.exists():
        print(f"[重下] 按 --redownload 删除已有文件 {dest}")
        dest.unlink()

    have = dest.stat().st_size if dest.exists() else 0
    if expected and have == expected:
        print(f"[跳过下载] {dest} 已是完整文件（{mib(have)}）")
        print(f"           如需重新下载：加 --redownload，或删除该文件")
        return dest

    req = urllib.request.Request(url, headers=({"Range": f"bytes={have}-"} if have else {}))
    print(f"[下载] {url}")
    if have:
        print(f"       发现残片 {mib(have)}，尝试断点续传")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        raise SystemExit(f"下载失败：{e}\n"
                         f"  可手动下载后放到 {dest} 再重跑本脚本（会自动复用）")

    with resp:
        clen = resp.headers.get("Content-Length")
        if resp.status == 206:
            total = have + int(clen) if clen else (expected or 0)
            mode, initial = "ab", have
        else:
            if have:
                print("       [!] 服务端未返回 206，不支持续传，改为整文件重下")
            total = int(clen) if clen else (expected or 0)
            mode, initial = "wb", 0
        with open(dest, mode) as f, tqdm(
                total=total or None, initial=initial, unit="B", unit_scale=True,
                unit_divisor=1024, desc="下载 minidev.zip", ncols=88) as bar:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                bar.update(len(chunk))

    size = dest.stat().st_size
    if expected and size != expected:
        raise SystemExit(f"下载校验失败：{size} 字节 ≠ 期望 {expected} 字节。\n"
                         f"  请删除 {dest} 后重跑（或加 --expected-bytes 0 跳过校验）")
    print(f"[下载完成] {mib(size)}  ->  {dest}")
    return dest


# ---------------------------------------------------------------- 选择性解压
def _wanted(name):
    """只收 dev_databases/** 与 3 个 SQLite 小文件。"""
    if not name.startswith(ZIP_PREFIX):
        return False
    rel = name[len(ZIP_PREFIX):]
    if not rel:
        return False
    return rel.startswith("dev_databases/") or rel in FROM_ZIP_SMALL


def _safe_rel(rel):
    """zip-slip 防护：拒绝绝对路径与上跳。"""
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise RuntimeError(f"zip 内出现非法路径，已拒绝：{rel}")
    return rel


def extract_selected(zip_path, tmp_root):
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True)

    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if _wanted(m.filename)]
        if not any(m.filename.endswith("dev_databases/") for m in members):
            raise SystemExit(f"{zip_path} 里找不到 {ZIP_PREFIX}dev_databases/，"
                             f"zip 可能不完整。")
        total = sum(m.file_size for m in members)
        print(f"[解压] 选中 {len(members)} 个条目 / 未压缩 {mib(total)}"
              f"（mysql、postgresql 的 BIRD_dev.sql 不落盘）")
        with tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                  desc="解压", ncols=88) as bar:
            for m in members:
                rel = _safe_rel(m.filename[len(ZIP_PREFIX):])
                out = tmp_root / rel
                if m.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(out, "wb") as dst:
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        dst.write(b)
                        bar.update(len(b))
    return tmp_root


# ---------------------------------------------------------------- 摆正
def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def install(tmp_root):
    src = tmp_root / "dev_databases"
    if not src.is_dir():
        raise SystemExit("解压结果中缺少 dev_databases/，中止。")
    dst = config.DB_DIR
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    if dst.exists():
        aside = config.DATA_DIR / (TMP_NAME + "_old")
        if aside.exists():
            shutil.rmtree(aside)
        os.rename(dst, aside)              # 先挪开，命名冲突时不至于丢数据
        try:
            os.rename(src, dst)            # 同卷重命名，瞬时
        except Exception:
            os.rename(aside, dst)          # 回滚
            raise
        shutil.rmtree(aside)
        print(f"[替换] {dst}（旧目录已删除）")
    else:
        os.rename(src, dst)
        print(f"[摆正] dev_databases -> {dst}")

    for f in FROM_ZIP_SMALL:
        d, s = config.DATA_DIR / f, tmp_root / f
        if d.exists():
            if s.exists() and _sha256(d) != _sha256(s):
                print(f"[警告] {f} 已存在且与下载包内容不同，保持原样（未覆盖）")
            else:
                print(f"[保留] {f}（仓库自带，内容一致）")
        elif s.exists():
            shutil.copy2(s, d)
            print(f"[补齐] {f}")
    for f in REQUIRED_SMALL:
        if not (config.DATA_DIR / f).exists():
            print(f"[缺失] {f}：不在 zip 内且仓库里也没有，请检查 clone 是否完整")


# ---------------------------------------------------------------- 清理
def cleanup(tmp_root, zip_path, keep_zip):
    """只删白名单内的东西：临时目录、.DS_Store、zip。"""
    root = config.ROOT.resolve()
    tmp = tmp_root.resolve()
    if tmp != root and root in tmp.parents:
        if tmp.exists():
            shutil.rmtree(tmp)
            print(f"[清理] 临时目录 {tmp_root}")
    else:
        raise RuntimeError(f"拒绝删除越界路径：{tmp_root}")

    n = 0
    for ds in config.DB_DIR.rglob(".DS_Store"):
        ds.unlink()
        n += 1
    if n:
        print(f"[清理] {n} 个 .DS_Store")

    zp = Path(zip_path).expanduser()
    if keep_zip:
        print(f"[保留] {zp}（--keep-zip）")
    elif zp.exists():
        size = zp.stat().st_size
        zp.unlink()
        print(f"[清理] {zp}（释放 {mib(size)}）")


# ---------------------------------------------------------------- 自检
def self_check():
    print("=" * 68)
    print("[自检] 目标结构")
    ok = report_state()
    print("=" * 68)
    print("自检 " + ("通过 ✅" if ok else "未通过 ❌"))
    if ok:
        print("\n下一步：")
        print(f"  {sys.executable} -m db_agent gate 1")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="下载并摆正 BIRD Mini-Dev (SQLite) 数据集到 data/")
    ap.add_argument("--url", default=URL, help="zip 下载地址")
    ap.add_argument("--zip", default=str(DEFAULT_ZIP),
                    help=f"zip 落点（默认 {DEFAULT_ZIP}）")
    ap.add_argument("--expected-bytes", type=int, default=EXPECTED_BYTES,
                    help="zip 期望字节数；0 = 关闭校验")
    ap.add_argument("--keep-zip", action="store_true", help="保留下载的 zip")
    ap.add_argument("--force", action="store_true", help="目标已完整时也重建")
    ap.add_argument("--redownload", action="store_true", help="忽略本地 zip，从头下载")
    a = ap.parse_args()

    print("=" * 68)
    print("fetch_data · BIRD Mini-Dev (SQLite) -> data/dev_databases")
    print("=" * 68)

    print("[检查] 目标现状")
    ok = report_state()
    if ok and not a.force:
        print("\n目标已完整，无需下载。要强制重建请加 --force。")
        return 0
    if ok:
        print("\n[--force] 目标已完整，但仍将重建。")

    free = shutil.disk_usage(config.ROOT).free
    zip_exists = Path(a.zip).expanduser().exists()
    need = (0 if zip_exists else a.expected_bytes) + 1600 * 1024**2
    if free < need:
        print(f"[警告] 磁盘余量 {free / 1024**3:.1f} GiB 可能不足"
              f"（预计需要约 {need / 1024**3:.1f} GiB）")

    zip_path = download(a.url, a.zip, a.expected_bytes, a.redownload)
    tmp_root = config.DATA_DIR / TMP_NAME
    extract_selected(zip_path, tmp_root)
    install(tmp_root)
    cleanup(tmp_root, zip_path, a.keep_zip)
    print()
    return self_check()


if __name__ == "__main__":
    sys.exit(main())
