# UniAPI - OpenAI API转发器

UniAPI是一个兼容OpenAI协议的API转发器，允许用户管理多个API密钥并在请求时根据模型随机选择合适的密钥。

## 功能特点

- 支持OpenAI API和兼容OpenAI协议的其他服务 (如Azure OpenAI, Claude API等)
- 自定义API密钥、Base URL和模型列表
- 支持模型映射，可以使用统一的模型名称映射到不同厂商的实际模型
- 请求时根据模型自动随机选择API密钥
- 支持流式和非流式输出
- 标准Bearer Token认证，与OpenAI API完全兼容
- 在Vercel上轻松部署
- 安全的管理员登录系统，保护您的API配置

## 安装和使用

### 本地运行

1. 克隆仓库
```bash
git clone https://github.com/yourusername/uniapi.git
cd uniapi
```

2. 安装依赖
```bash
pip install -r requirements.txt
```

3. 设置环境变量（可选，开发模式下有默认值）
```bash
# Windows PowerShell
$env:API_KEY_1="your_api_key_1"
$env:API_KEY_2="your_api_key_2"
$env:ADMIN_API_KEY="your_admin_key"  # 默认为 "adminadmin"

# Linux/macOS
export API_KEY_1="your_api_key_1"
export API_KEY_2="your_api_key_2"
export ADMIN_API_KEY="your_admin_key"  # 默认为 "adminadmin"
```

4. 运行服务
```bash
python main.py
```

服务将在 http://localhost:8000 上运行。

### 角色与权限

UniAPI有两种类型的API密钥：

1. **管理员API密钥**（ADMIN_API_KEY）:
   - 可以访问管理面板
   - 可以管理API配置和模型映射
   - 可以调用API

2. **普通API密钥**（API_KEY_1到API_KEY_5）:
   - 只能调用API
   - 不能访问管理面板
   - 不能管理API配置和模型映射

### 本地测试

在本地测试时，可以使用以下方法：

1. 访问管理界面：
   - 打开浏览器访问 http://localhost:8000
   - 使用管理员API密钥登录（默认为 `adminadmin`）

2. 调用API:
   - 使用curl或其他HTTP客户端
   - 支持的端点: `/v1/chat/completions`
   - 使用开发API密钥（在非生产环境）：`dev_api_key_1`或`dev_api_key_2`

```bash
# 测试聊天完成
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev_api_key_1" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

3. 在应用程序中配置:
   - 将您的OpenAI客户端库指向 `http://localhost:8000`
   - 使用开发API密钥作为认证令牌
   - 注意：当前版本只支持 `/v1/chat/completions` 端点

### Vercel部署

1. Fork此仓库
2. 在Vercel上创建新项目并导入该仓库
3. 配置环境变量：
   - `ADMIN_API_KEY`: 管理员API密钥，用于访问管理面板（必须设置）
   - `API_KEY_1` 到 `API_KEY_5`: 配置1-5个允许访问的API密钥（至少配置一个）
   - `REDIS_URL`: Redis连接URL（如果要持久化存储配置）
   - `ENVIRONMENT`: 设置为`production`以禁用开发模式下的默认API密钥

部署完成后，你将获得一个Vercel提供的URL。

## 安全访问

为了保护您的API配置不被未授权访问，所有请求都需要包含有效的API密钥。API密钥需要使用OAuth Bearer Token格式在授权头中提供：

```
Authorization: Bearer your_api_key
```

您可以通过环境变量`API_KEY_1`到`API_KEY_5`配置最多5个允许的API密钥，这些密钥可以用于调用API但不能访问管理面板。
管理员API密钥可以通过环境变量`ADMIN_API_KEY`设置，默认值为`adminadmin`。

## API使用说明

### 支持的端点

当前版本仅支持以下端点:
- `POST /v1/chat/completions` - 创建聊天完成

### 请求参数

请求参数与OpenAI官方API一致，主要包括：
- `model`: 要使用的模型名称
- `messages`: 消息数组，包含role和content
- `temperature`: 温度参数，控制随机性
- `max_tokens`: 生成的最大token数
- `stream`: 是否使用流式输出

### 示例请求

```bash
curl https://your-vercel-url.vercel.app/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_api_key" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

## 模型映射功能

UniAPI支持模型映射，允许您使用统一的模型名称映射到不同厂商的实际模型。例如，您可以将 `gpt-4` 映射到不同厂商的相应模型，然后在请求中使用 `gpt-4` 作为模型名称，系统会自动处理映射。

## 许可证

本项目采用 Apache License 2.0 许可证。详情见LICENSE文件。
