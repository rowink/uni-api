# UniAPI - OpenAI API转发器

UniAPI是一个兼容OpenAI协议的API转发器，允许用户管理多个API密钥并在请求时根据模型随机选择合适的密钥。

## 效果展示
### 添加配置
![image](https://github.com/user-attachments/assets/297461f8-1d4a-40ab-9e36-ae7a1da3dae7)
### 配置列表
![image](https://github.com/user-attachments/assets/bb9d3bef-da29-467f-b722-2287aa570c08)
### vercel环境变量
![image](https://github.com/user-attachments/assets/6e9fc577-e8c2-4693-a677-614b7328b0ed)



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


### Vercel一键部署
[![Deploy with Vercel](https://vercel.com/button)](https%3A%2F%2Fvercel.com%2Fnew%2Fclone%3Frepository-url%3Dhttps%3A%2F%2Fgithub.com%2Fzhangtyzzz%2Funi-api%26env%3DADMIN_API_KEY%2CTEMP_API_KEY%2CENVIRONMENT%26envDescription%3D%60ADMIN_API_KEY%60%3A%20%E7%AE%A1%E7%90%86%E5%91%98API%E5%AF%86%E9%92%A5%EF%BC%8C%E7%94%A8%E4%BA%8E%E8%AE%BF%E9%97%AE%E7%AE%A1%E7%90%86%E9%9D%A2%E6%9D%BF%EF%BC%88%E5%BF%85%E9%A1%BB%E8%AE%BE%E7%BD%AE%EF%BC%89%20
%60TEMP_API_KEY_ONE%60%20%E5%92%8C%20%60TEMP_API_KEY%60%3A%20%E9%85%8D%E7%BD%AE2%E4%B8%AA%E5%85%81%E8%AE%B8%E8%AE%BF%E9%97%AE%E7%9A%84API%E5%AF%86%E9%92%A5
%60REDIS_URL%60%3A%20Redis%E8%BF%9E%E6%8E%A5URL%EF%BC%88%E5%A6%82%E6%9E%9C%E8%A6%81%E6%8C%81%E4%B9%85%E5%8C%96%E5%AD%98%E5%82%A8%E9%85%8D%E7%BD%AE%EF%BC%89
%20%60ENVIRONMENT%60%3A%20%E8%AE%BE%E7%BD%AE%E4%B8%BA%60production%60%E4%BB%A5%E7%A6%81%E7%94%A8%E5%BC%80%E5%8F%91%E6%A8%A1%E5%BC%8F%E4%B8%8B%E7%9A%84%E9%BB%98%E8%AE%A4API%E5%AF%86%E9%92%A5)

配置环境变量：
   - `ADMIN_API_KEY`: 管理员API密钥，用于访问管理面板（必须设置）
   - `TEMP_API_KEY_ONE` 和 `TEMP_API_KEY`: 配置2个允许访问的API密钥
   - `REDIS_URL`: Redis连接URL（如果要持久化存储配置）
   - `ENVIRONMENT`: 设置为`production`以禁用开发模式下的默认API密钥

部署完成后，你将获得一个Vercel提供的URL，使用ADMIN_API_KEY登录并录入API即可。

## 安全访问

为了保护您的API配置不被未授权访问，所有请求都需要包含有效的API密钥。API密钥需要使用OAuth Bearer Token格式在授权头中提供：

```
Authorization: Bearer your_api_key
```

您可以通过环境变量`TEMP_API_KEY_ONE`和`TEMP_API_KEY`配置最多2个额外的API密钥，这些密钥可以用于调用API但不能访问管理面板。
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
