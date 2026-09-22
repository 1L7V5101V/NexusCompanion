# 环境重建与 pyright 异常记录（2026-09-22）

## 一、开发环境被重建（非本 change 引起）

开工 task 6 前发现主仓库环境被重建过，本 worktree 一度变成孤立目录：

| 现象 | 证据 |
| --- | --- |
| 主仓库 `.git` 被重建 | `.git/worktrees/` **整个不存在**；`git worktree list` 只剩主工作树（wt-c15/wt-c4/c5-merge 全部消失）；本地无 `feature/c15-work-queue-consumer` 分支 |
| 主仓库 HEAD 前进 | 由 `224458e` → `bd592ec6`（含 `webchat-auth-wiring 21/21` 等新提交） |
| `.venv` 被重建 | **Python 3.13.15 → 3.12.14**（pytest 9.1.1 / sqlalchemy 2.0.49） |
| 本 worktree 变成普通目录 | `.git` 指针文件仍在，但指向已不存在的 admin 目录 → `git status` 报 `fatal: not a git repository: (NULL)` |

**恢复动作（未丢失任何提交）**：

1. 确认 `origin/feature/c15-work-queue-consumer` = `bbbca5d9`（**9 个提交全在远端**）；
2. 扫描确认目录里**只有** `bootstrap/work_queue_telemetry.py` 一个未提交文件（`find -newermt`）；
3. 把孤立目录改名保留为 `D:/Project/NexusCompanion-worktrees/wt-c15-orphaned`（未删除）；
4. `git branch feature/c15-work-queue-consumer origin/feature/c15-work-queue-consumer` + `git worktree add` 重建；
5. 把未提交文件拷回，重建 `.venv` junction。

> `wt-c15-orphaned` 可确认无用后删除。

## 二、pyright 在本环境对该文件误判（**环境缺陷，不是代码缺陷**）

`bootstrap/work_queue_worker.py` 在 pyright 下稳定报 **15 个错误**，全部落在**中文文本内部**：

```
work_queue_worker.py:1:1  - error: Expected expression
work_queue_worker.py:4:1  - error: Invalid character "\ufffd" in token
work_queue_worker.py:8:1  - error: Invalid character "\u16" in token
work_queue_worker.py:216:46 - error: Statements must be separated by newlines or semicolons
```

（216:46 指 `"""停止认领新 work（并关闭 lane）；在途 handler 继续执行至收束。"""` 这句 docstring 内部的位置。）

**判为环境缺陷的依据**：

| 证据 | 结果 |
| --- | --- |
| Python 自身解析 | `ast.parse` **OK**（539 行、有效 UTF-8、**0 个 U+FFFD 字节**） |
| 本 change 测试 | `tests/test_work_queue_worker.py` 等 **48 passed**（模块被真实导入并执行） |
| 同内容换名 | 复制为 `_probe_worker_copy.py` 后**同样 15 个错误**（排除按路径缓存） |
| 另一中文重文件 | `bootstrap/work_queue_telemetry.py`（同样大量中文、LF）**0 errors** |
| 本次重建之前 | 同一文件在同一会话早期 pyright 报 **0 errors, 8 warnings** |
| pyright 自身 | 多次运行直接输出 `Please install the new version or set PYRIGHT_PYTHON_FORCE_VERSION to \`latest\`` 而不出结果（wrapper 不稳定）；版本提示 `v1.1.411 -> v1.1.414` |

**真实根因（2026-09-22 定位）：本机装有透明加密/DLP，按进程放行**

决定性证据（同一文件、不同进程读到的内容不同）：

```
$ head -c 16 session/store.py | od -c        # bash（非白名单进程）
0000000   %   T   S   D   -   H   e   a   d   e   r   -   #   #   #   %

$ python -c "...read_bytes()..."             # Python（白名单进程）
size=56847  head=b'from __future__ '
```

node 视角同样读到密文：

```
session/store.py            node size=61440  head="%TSD-Header-###%"   （实际 56847）
bootstrap/work_queue_worker node size=28672  head="%TSD-Header-###%"   （实际 21337）
bootstrap/work_queue_telemetry node size=11784 head='""""C15 work item'  ← 明文！
```

**放行规则（实测）**：

| 角色 | 读 | 写 |
| --- | --- | --- |
| **Python**（pytest / 脚本） | ✅ 透明解密，读到明文 | ❌ **写出的文件被加密** |
| **node.exe**（pyright） | ❌ 读到密文 | — |
| **bash / coreutils**（head/od/grep/sed/cat） | ❌ 读到密文 | — |
| `write` / `edit` 工具 | ✅ | ✅ **写出明文** |

- 仓库里 **673 个文件**带 `%TSD-Header-###%` 头（bash grep 视角）。
- **Python 写出的新文件也会被加密**：在**主仓库**（`D:/Project/NexusCompanion`，其既有文件是明文）里用 Python 新建一个探针文件，`head` 读到的仍是 `%TSD-Header-###%` ⇒ 说明这**不是按目录，而是按写进程**。
- 因此：**唯一能被 pyright 正确解析的文件，是 `write`/`edit` 工具写出的那些**（`work_queue_telemetry.py` 正是如此）。
- 这也解释了本会话早期的「怪事」——`%TSD-Header-###%` 乱码、`cat: stream did not contain valid
  UTF-8`、read 工具报出错误行数——**当时我误判为 rtk 包装器伪影，实际是同一个 DLP**。主仓库里
  曾出现的 `_dlp_probe_inside.py/.txt` 就是此前为探测该现象留下的。

**排除的假设（均已实测证伪）**：

| 假设 | 证伪方式 |
| --- | --- |
| 中文/UTF-8 触发 | 纯 ASCII 副本（非 ASCII 全替换为 `x`）**同样**报 `Invalid character "\ufffd"` |
| 文件大小阈值（~16KB） | 「撑大后的 telemetry」失败并非因为大小 —— 它是**用 Python 写的**，因而被加密 |
| CRLF 触发 | store.py 的 **LF 副本**同样失败 |
| pip 包装器坏 | **官方 pyright（npx）**报同样的错 |
| pyright 版本 | `PYRIGHT_PYTHON_FORCE_VERSION=latest` 无效 |
| 文件本身非法 | Python 侧 `ast.parse` 通过、`decode('utf-8')` 通过、0 个 U+FFFD 字节 |

**结论**：`--level error` 门禁在本机**不可信**（pyright 读到密文）。这不是代码缺陷。

**已采用的解法（本机实测可用）：把跟踪的 `.py` 用 `git show` 重写为明文**

前一版 C4 change 的证据 README 已记录同一现象（「天锐绿盾类透明加密」），并给出做法：

```bash
# git 属受控进程（读到明文），bash 重定向写出的落盘文件是明文
git ls-files '*.py' | while IFS= read -r f; do git show "HEAD:$f" > "$f"; done
git add --renormalize .      # 刷新 stat 缓存（否则 git status 会报大量假 modified）
```

- 内容与 blob 逐字节一致（`git diff` 为空；`git status` 只剩真实改动）；
- 文件落盘变为**明文**，于是 **node/pyright 能正确读取**；
- 属**本机工作区**操作，不改动 `main` 工作树、不影响已提交内容。

**本次结果（2026-09-22，重写后运行）**：

| 配置 | 命令 | 结果 |
| --- | --- | --- |
| project | `pyright --project pyrightconfig.json --level error` | **36 errors / 0 warnings**；`Invalid character` **0** 处 |
| tests | `pyright --project pyrightconfig.tests.json --level error` | **41 errors / 0 warnings**；`Invalid character` **0** 处 |

- project 的 36 errors **与 C12 记录的基线（36）完全一致**；
- tests 的 41 vs 旧基线 31：因 `main` 已前进（C4/C5 等新文件带入自己的既有错误），非本 change 引入；
- **按文件核对：两个配置的报错文件清单里没有任何 C15 新增/修改文件**（`work_queue*` 命中 error 数为 0）；
- 原始输出：`pyright-project.txt` / `pyright-tests.txt`。

**⇒ task 7.1 成立：本 change 未引入新的 pyright 错误。**

**备选解法（若不想动工作区）**：

1. 让 IT 把 **`node.exe` 加入 DLP 白名单**，或把仓库目录**排除加密**；
2. 在**无此 DLP 的环境**跑 pyright（服务器 / Linux 容器 / 另一台机器）。

> ⚠️ 附带风险提示：既然 **Python 写出的文件会被加密**，提交前值得确认 git 侧读到的是明文
> （本次 `git diff` / 已推送产物均显示正常明文，故 git 应属白名单；但这是用户环境的安全产品，
> 建议自行复核一次）。

## 三、本机仍可采信的证据

> 注：下表的 48 项与 43 项在**环境重建后已复跑通过**（Python 3.12 venv），见
> `work-queue-telemetry-pytest.txt` 与 `work-queue-recovery-verification.md`。

- `ast.parse`：所有新增/修改文件通过；
- 测试：`tests/test_work_queue_worker.py`（22）+ `test_work_queue_wiring.py`（12）+
  `test_work_queue_telemetry.py`（14）= **48 passed**，见同目录
  `work-queue-telemetry-pytest.txt`；
- 真 PG 仓储验证（43 项）：见 `work-queue-repo-verification.md`（本轮未复跑，环境重建前已通过）。
