# UniAPI - OpenAI API转发器

UniAPI是一个兼容OpenAI协议的API转发器，允许用户管理多个API密钥并在请求时随机选择合适的密钥。

## 功能特点

- 支持OpenAI API和兼容OpenAI协议的其他服务
- 自定义API密钥、Base URL和模型列表
- 请求时根据模型自动随机选择API密钥
- 支持流式和非流式输出
- 支持reasoningContent字段
- 标准Bearer Token认证，与OpenAI API完全兼容
- 在Vercel上轻松部署
- 安全的登录系统，保护您的API配置

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

# Linux/macOS
export API_KEY_1="your_api_key_1"
export API_KEY_2="your_api_key_2"
```

4. 运行服务
```bash
python main.py
```

服务将在 http://localhost:8000 上运行。

### 本地测试

在本地测试时，可以使用以下方法：

1. 使用浏览器访问管理界面：http://localhost:8000
2. 使用默认的管理API密钥（在非生产环境）：`adminadmin`
3. 使用curl或其他工具测试API：
4. 支持配置5个请求密钥用于对外分享：测试使用：dev_api_key_1，dev_api_key_2，dev_api_key_3，dev_api_key_4，dev_api_key_5，dev_api_key_6，

```bash

# 测试聊天
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev_api_key_1" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

4. 在应用程序中配置：
   - 将您的OpenAI客户端库指向 `http://localhost:8000`
   - 使用开发API密钥作为认证令牌

### Vercel部署

1. Fork此仓库
2. 在Vercel上创建新项目并导入该仓库
3. 配置环境变量：
   - `API_KEY_1` 到 `API_KEY_5`: 配置1-5个允许访问的API密钥（至少配置一个）
   - `REDIS_URL`: Redis连接URL（如果要持久化存储配置）
   - `ENVIRONMENT`: 设置为`production`以禁用开发模式下的默认API密钥

部署完成后，你将获得一个Vercel提供的URL。

## 安全访问

为了保护您的API配置不被未授权访问，所有请求都需要包含有效的API密钥。API密钥需要使用OAuth Bearer Token格式在授权头中提供：

```
Authorization: Bearer your_api_key
```

您可以通过环境变量`API_KEY_1`到`API_KEY_5`配置最多5个允许的API密钥。

## API使用说明

### 使用API（与OpenAI兼容）

所有发往 `/v1/chat/completions` 的请求将被转发到相应的API提供商，自动选择合适的API密钥。

示例:
```bash
curl https://your-vercel-url.vercel.app/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_api_key" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

## 许可证

本项目采用MIT许可证。详情见LICENSE文件。
