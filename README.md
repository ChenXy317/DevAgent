# Coding Agent

个人单机使用的 Web coding agent。页面里管理会话和模型设置，后端把对话存在本机 MySQL。文件和命令的目标目录是每个会话自己的工作目录。

当前可以管理会话、模型配置和放行规则。对话会按工具循环执行：读文件、修改、运行命令、联网搜索，以及在用户点击时压缩更早的上下文。`write`、`edit` 和 `shell` 会先请求批准。

## 准备

需要本机 MySQL、uv 和 Node.js。库要事先建好。复制环境文件后填写连接信息：

```bash
cp .env.example backend/.env
```

`LLM_API_KEY` 和 `SEARCH_API_KEY` 留在 `backend/.env` 或设置页里，不写入仓库。模型密钥留空表示不修改已保存的值。

## 启动

```bash
./start.sh
```

页面在 `http://127.0.0.1:5173`，接口在 `http://127.0.0.1:8000`。脚本会先停掉这两个端口上的旧进程。Ctrl+C 关掉后端和前端。

后端检查：

```bash
cd backend && uv run pytest
```

前端检查：

```bash
cd frontend && npm run typecheck && npm run lint
```
