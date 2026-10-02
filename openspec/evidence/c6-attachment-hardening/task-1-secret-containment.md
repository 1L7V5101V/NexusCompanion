# Task 1.1 — 运行期密钥不再入库（2026-10-02）

## 背景

C6 Task 4 的 HTTP 测试以仓库根作为 AuthRuntime 的 workspace，`PepperProvider` 在
`workspace/secrets/` 生成 pepper 文件，被 `146736d2`（feat(c6): uploads/media 端点）连带提交：

```
$ git log --oneline --diff-filter=A -- secrets/auth_pepper
146736d2 feat(c6): uploads/media 端点 BREAKING 改造（attachment_id 契约 + ownership + 移除 /tmp fallback）+ 前端构建适配
```

`.gitignore` 原本没有任何 `secrets/` 规则。该 commit 随 C6 合入**本地 main，但未推送**
（`git branch -r --contains 146736d2` 为空），因此值尚未扩散到远端。

## 处置

- `git rm --cached secrets/auth_pepper`：只取消跟踪，**本地文件保留**（pepper 变更会使既有
  session digest 全部失效，属运行期数据，不可随意轮换）。
- `.gitignore` 追加：

```
# 运行期生成的密钥材料（pepper 等），任何情况下不入库
/secrets/
**/auth_pepper
```

## 验证

```
$ git check-ignore -v secrets/auth_pepper
.gitignore:6:/secrets/	secrets/auth_pepper

$ git ls-files secrets/ | wc -l
0

$ ls secrets/
auth_pepper          # 文件仍在本地，digest 校验链不变
```

提交：`7aac6494 fix(security): 停止跟踪运行期生成的 auth pepper`（独立于附件改动，
不随附件 revert 一起撤销）。

## 遗留（需决策，未擅自动手）

`secrets/auth_pepper` 仍存在于本地 git 历史（`146736d2..bc24797f`，均未推送）。若希望历史里
也不留痕，需在 push 前对这 8 个 commit 做本地历史改写（`git filter-repo` 或 interactive
rebase）。这属破坏性操作且会改写 commit hash，故留给所有者确认；由于从未推送，远端与协作者
不受影响，风险窗口只在「误推」这一步——本 change 已把该步拦住（ignore + 未跟踪）。
