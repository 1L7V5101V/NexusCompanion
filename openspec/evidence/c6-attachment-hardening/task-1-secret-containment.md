# Task 1.1 — 运行期密钥不再入库（2026-10-02）

## 背景

C6 Task 4 的 HTTP 测试以仓库根作为 AuthRuntime 的 workspace，`PepperProvider` 在
`workspace/secrets/` 生成 pepper 文件，被 uploads/media 端点那个 commit 连带提交（历史改写后为 `47778932`，原 `146736d2`）：

```
$ git log --oneline --diff-filter=A -- secrets/auth_pepper
146736d2 feat(c6): uploads/media 端点 BREAKING 改造…  ← 改写前哈希，现对应 47778932
```

`.gitignore` 原本没有任何 `secrets/` 规则。该 commit 随 C6 合入**本地 main，但未推送**
（当时本地跟踪 ref 显示该 commit 未出现在任何远端 ref 中），因此值尚未扩散到远端。

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

提交：`fix(security): 停止跟踪运行期生成的 auth pepper`（改写后 `45ad61a4`，原 `7aac6494`；
独立于附件改动，不随附件 revert 一起撤销）。

## 历史抹除（2026-10-02，经所有者确认后执行）

所有者选择「先抹掉再 push」，故在 push 前执行：

```
git filter-repo --invert-paths --path secrets/auth_pepper --force   # 对 already_ran 提示答 n（全新运行）
```

验证：
- `git log --all --oneline -- secrets/auth_pepper` → 空（任何 ref 的历史都不再含该路径）；
- 与改写前 tip 做 `git diff` → **空输出**（树逐字节一致，改写只动 commit 哈希，不动内容）；
- 生产 pepper 为另一文件另一值（`/root/.nexus/workspace/secrets/auth_pepper`，长度 65，
  与本地泄露值长度 66 不同）→ 线上会话不受影响；
- 改写前状态已备份：`D:/1/c6-pre-rewrite-backup.bundle`（注意该 bundle 仍含该值，勿外传，
  确认无误后可删）。

## 顺带查出的谱系分叉（重要，非本 change 引入）

`git fetch origin main` 后发现：远端 main（`7a2bc1ff`）**不是**本地 main 的祖先。共同祖先是
`7c358258`（2026-07-13 18:44），正好与 `.git/filter-repo/already_ran`（2026-07-13 18:42）同一时刻
——即本地在 7-13 被 filter-repo 改写出一条**从未推送**的平行谱系（本地独有 425 commit，
远端独有 412 commit）。后果：

- 推送 main 不是 fast-forward，需 `--force-with-lease`（远端内容经 `git diff --name-status`
  核对为本地的严格子集，无远端独有文件，覆盖不丢工作）；
- 本地其他分支（c4/c5/c8/c12/gemini/pulsecore）tip 也随之与远端不同（tree 已核对一致，仅哈希漂移）；
- openspec 文档/records 中 2026-07-13 之前写下的 commit 哈希引用全部与远端不一致，
  需按 `.git/filter-repo/commit-map` 重映射（本次只修了自己新写的引用，历史文档未批量改动）。
