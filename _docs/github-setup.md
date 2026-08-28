# GitHub Setup

本仓库应该推到一个私有 GitHub repo，例如：

```text
MenikeKassel/ai-hub
```

## 推荐方式：GitHub CLI

安装并登录 GitHub CLI 后运行：

```powershell
gh auth login
python D:\aiworkspace\ai-hub\scripts\check_review_bundle.py
gh repo create MenikeKassel/ai-hub --private --source D:\aiworkspace\ai-hub --remote origin --push
```

## 网页方式

1. 在 GitHub 创建 private repository：`ai-hub`。
2. 不要初始化 README、.gitignore 或 license，因为本地仓库已经有初始提交。
3. 回到本机运行：

```powershell
python D:\aiworkspace\ai-hub\scripts\check_review_bundle.py
git -C D:\aiworkspace\ai-hub remote add origin https://github.com/MenikeKassel/ai-hub.git
git -C D:\aiworkspace\ai-hub push -u origin main
```

## 注意

- 不要提交 `.env`、API key、飞书 secret、Notion 原始导出、整个 Obsidian vault。
- GitHub 仓库只放自动化代码、模板和系统说明。
