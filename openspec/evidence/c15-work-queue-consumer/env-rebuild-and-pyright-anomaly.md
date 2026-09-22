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

**决定性探针：把该文件的非 ASCII 字符全部替换成 `x`（纯 ASCII 副本）后，pyright 仍报 137 个错误、仍含 `Invalid character "\ufffd"`。**

这**排除**了「中文/UTF-8 触发」这一解释——pyright 在一个纯 ASCII 文件上也会报 `\ufffd`（0xEF 0xBF 0xBD，即解码失败占位符），且错误数量在多次运行间不稳定（同一文件先后得到 15 / 57 / 137 / 184）。

字符集对比也排除了「文件含特殊字符」：该文件除 ASCII 外只有常规 CJK 字符，**无任何控制字符或非法码点**。

**结论**：本环境（重建后的 `pyright-python` 包装器 + Python 3.12 venv）**读文件本身不可靠**，`--level error` 门禁在本机**不可信**。

**因此**：

- 本 change 的 pyright 证据**不能在本机采信**；task 7.1 须在干净环境（CI / 未重建的 venv）复跑；
- 已排除代码侧原因：`ast.parse` 干净、48 项测试通过、同内容换路径同样报错、中文重文件可过。

## 三、本机仍可采信的证据

> 注：下表的 48 项与 43 项在**环境重建后已复跑通过**（Python 3.12 venv），见
> `work-queue-telemetry-pytest.txt` 与 `work-queue-recovery-verification.md`。

- `ast.parse`：所有新增/修改文件通过；
- 测试：`tests/test_work_queue_worker.py`（22）+ `test_work_queue_wiring.py`（12）+
  `test_work_queue_telemetry.py`（14）= **48 passed**，见同目录
  `work-queue-telemetry-pytest.txt`；
- 真 PG 仓储验证（43 项）：见 `work-queue-repo-verification.md`（本轮未复跑，环境重建前已通过）。
