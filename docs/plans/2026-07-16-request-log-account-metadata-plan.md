# 请求日志账户元数据实施计划

> 按已确认设计直接实施。遵循测试驱动：每个行为先写测试并确认失败，再写最小实现使其通过。按项目约定，本计划不执行 git add、commit 或 push。

## 任务一：账户池暴露请求局部安全元数据

**文件：**

- 新增：`tests/test_oauth_pool.py`
- 修改：`codex_api_service/oauth_pool.py`

**步骤：**

1. 写测试验证非流式和流式请求完成后可读取 `account_key`、`account_alias`。
2. 写测试验证请求失败时保留最后一次尝试的账户。
3. 运行 `pytest -q tests/test_oauth_pool.py`，确认因能力缺失而失败。
4. 使用 `ContextVar` 实现请求局部账户元数据，并在每次选中账户时更新。
5. 重跑测试直至通过。

## 任务二：请求日志持久化账户字段

**文件：**

- 修改：`tests/test_admin.py`
- 修改：`codex_api_service/request_log.py`

**步骤：**

1. 扩展持久化测试，要求写入和加载账户键及账户别名。
2. 扩展旧日志测试，要求缺失字段时返回 `None`。
3. 运行 `pytest -q tests/test_admin.py -k request_log`，确认新断言失败。
4. 扩展日志数据类、序列化、反序列化和记录接口。
5. 重跑相关测试直至通过。

## 任务三：所有请求路径写入账户元数据

**文件：**

- 修改：`tests/test_app.py`
- 修改：`codex_api_service/app.py`

**步骤：**

1. 增加账户感知测试客户端。
2. 添加测试覆盖 Chat Completions、Responses、Anthropic 的成功日志，并覆盖流式和错误日志。
3. 运行新增测试，确认日志缺少账户字段。
4. 增加安全读取账户元数据的兼容助手，并接入所有请求日志写入点。
5. 重跑相关测试直至通过。

## 任务四：详情抽屉展示账户

**文件：**

- 修改：`frontend/src/types.ts`
- 修改：`frontend/src/App.test.tsx`
- 修改：`frontend/src/App.tsx`

**步骤：**

1. 在抽屉测试数据中加入账户字段并断言别名可见。
2. 增加旧日志缺少别名时显示“未记录”的断言。
3. 运行 `npm --prefix frontend test -- App.test.tsx`，确认失败。
4. 扩展 TypeScript 类型，并在“执行”区域展示账户。
5. 重跑前端测试直至通过。

## 任务五：回归验证与服务更新

**步骤：**

1. 运行后端全量测试：`.venv/bin/pytest -q`。
2. 运行前端全量测试：`npm --prefix frontend test`。
3. 构建前端：`npm --prefix frontend run build`。
4. 使用 `.venv/bin/codex-api-service restart` 更新现有后台服务。
5. 调用健康检查确认服务正常，不创建旧版 launchd 标签。
