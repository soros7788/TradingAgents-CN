#!/usr/bin/env python3
import subprocess
import sys

def run(cmd, check=True):
    print(f"$ {cmd}", flush=True)
    p = subprocess.run(cmd, shell=True, text=True)
    if check and p.returncode != 0:
        sys.exit(p.returncode)
    return p.returncode

def main():
    run("git fetch upstream")

    if run("git rev-parse --verify upstream/main", check=False) == 0:
        upstream_branch = "upstream/main"
    elif run("git rev-parse --verify upstream/master", check=False) == 0:
        upstream_branch = "upstream/master"
    else:
        print("错误：找不到 upstream/main 或 upstream/master")
        sys.exit(2)

    print(f"使用上游分支：{upstream_branch}")

    if run(f"git diff --quiet HEAD {upstream_branch}", check=False) == 0:
        print("当前已经是最新，无需同步。")
        sys.exit(0)

    code = run(f"git merge --no-edit {upstream_branch}", check=False)

    if code != 0:
        print("自动合并失败，可能有冲突。停止自动同步。")
        run("git status", check=False)
        sys.exit(2)

    print("自动同步完成。")
    sys.exit(0)

if __name__ == "__main__":
    main()
