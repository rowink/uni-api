import copy
import hashlib
import json
import logging
import os
import pathlib
import random
import time
import uuid
from collections import deque
from datetime import datetime
from typing import List, Dict, Optional

import httpx
import redis
from fastapi import FastAPI, Request, HTTPException, Depends, Security, status, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from api.models import ModelRequestRecord

UNKNOWN = 'unknown'

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger("uniapi")

# 获取当前文件的目录
BASE_DIR = pathlib.Path(__file__).parent.resolve()

app = FastAPI(title="UniAPI - OpenAI API转发器")

# 断路器规则，连续失败的次数，对应降级的时间，如果某个模型连续失败达到指定次数
# 会在最近的一次请求之后的x分钟内，直接降级，不再发起请求
fail_count_to_cooldown = {
    3: 5 * 60,  # 3次连续失败 -> 5分钟断路
    4: 10 * 60,  # 4次连续失败 -> 10分钟断路
    5: 30 * 60,  # 5次连续失败 -> 30分钟断路
    6: 2 * 60 * 60,  # 6次连续失败 -> 2小时断路
    7: 6 * 60 * 60,  # 7次连续失败 -> 6小时断路
    8: 24 * 60 * 60,  # 8次连续失败 -> 24小时断路
    9: 48 * 60 * 60,  # 9次连续失败 -> 48小时断路
}

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 配置模板
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 配置安全访问密钥，使用标准Bearer Token认证
security = HTTPBearer(auto_error=False)

# 从环境变量获取允许的API密钥
ALLOWED_API_KEYS = []

# 全局内存存储（当Redis不可用时使用）
model_request_history = {}

# 获取管理员API密钥
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "adminadmin")

# 获取HTTP请求超时设置
TIMEOUT_SECONDS = float(os.environ.get("TIMEOUT_SECONDS", "60"))
logger.info(f"HTTP请求超时设置为 {TIMEOUT_SECONDS} 秒")

# 默认开发API密钥（只用于本地开发，在生产环境中应该通过环境变量设置）
if os.environ.get("ENVIRONMENT") != "production":
    TEMP_API_KEY = os.environ.get("TEMP_API_KEY", "temp_api_key")
    TEMP_API_KEY_ONE = os.environ.get("TEMP_API_KEY_ONE", "temp_api_key_one")
    ALLOWED_API_KEYS = [TEMP_API_KEY, TEMP_API_KEY_ONE]
    logger.info(f"使用临时API密钥: {TEMP_API_KEY}, {TEMP_API_KEY_ONE}")
else:
    # 生产环境，只有明确设置了环境变量才使用
    TEMP_API_KEY = os.environ.get("TEMP_API_KEY")
    TEMP_API_KEY_ONE = os.environ.get("TEMP_API_KEY_ONE")
    if TEMP_API_KEY or TEMP_API_KEY_ONE:
        ALLOWED_API_KEYS = [key for key in [TEMP_API_KEY, TEMP_API_KEY_ONE] if key]
        logger.info(f"生产环境使用配置的API密钥，共 {len(ALLOWED_API_KEYS)} 个")


# 验证API密钥
async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
        # 如果没有配置任何API密钥，则禁止所有访问
        raise HTTPException(
            status_code=401,
            detail="未配置允许的API密钥。请在环境变量中设置TEMP_API_KEY或ADMIN_API_KEY"
        )

    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )

    api_key = credentials.credentials  # 从Bearer Token中提取API密钥

    # 检查是否是管理员API密钥
    if api_key == ADMIN_API_KEY:
        return api_key

    # 检查是否是普通API密钥
    if api_key in ALLOWED_API_KEYS:
        # 获取当前请求路径
        request = credentials.scope.get("request")
        if request:
            path = request.url.path
            # 如果是管理相关的API，拒绝访问
            if path.startswith("/admin") or path.startswith("/api/configs") or path.startswith("/api/model-mappings"):
                raise HTTPException(
                    status_code=403,
                    detail="普通API密钥无权访问管理功能"
                )
        return api_key

    raise HTTPException(
        status_code=401,
        detail="无效的API密钥"
    )


# 验证管理员API密钥
async def verify_admin_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )

    api_key = credentials.credentials
    if api_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="需要管理员权限"
        )

    return api_key


# 初始化Redis客户端
redis_url = os.getenv("REDIS_URL")
if redis_client := redis.from_url(redis_url) if redis_url else None:
    logger.info("Redis连接已配置")
else:
    logger.warning("Redis未配置，使用内存存储")
    # 使用内存存储作为备用
    in_memory_db = {
        "api_configs": [],
        "model_mappings": {}  # 新增模型映射存储
    }


# 模型映射数据模型
class ModelMapping(BaseModel):
    unified_name: str  # 统一的模型名称（用于外部调用）
    vendor_models: Dict[str, str]  # 厂商特定的模型名称映射，格式: {vendor_id: model_name}


# API配置数据模型
class APIConfig(BaseModel):
    id: Optional[str] = None
    api_key: str
    base_url: str
    models: List[str] = Field(..., description="支持的模型列表")
    created_at: Optional[str] = None
    vendor: Optional[str] = Field(None, description="厂商标识符，用于模型映射")
    model_mappings: Optional[Dict[str, str]] = Field(None, description="模型映射，格式: {统一模型名称: 实际模型名称}")


# 模型映射请求模型
class ModelMappingRequest(BaseModel):
    unified_name: str
    vendor_models: Dict[str, str]


# API配置相关端点
@app.post("/logout")
async def logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie(key="auth_key")
    response.delete_cookie(key="remember_auth")
    return response


# 使用cookie获取管理员密钥
async def get_admin_api_key_from_cookie(request: Request):
    auth_key = request.cookies.get("auth_key")
    if not auth_key or auth_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权，请使用管理员API密钥访问此页面"
        )
    return auth_key


@app.post("/api/configs")
async def create_config(config: APIConfig, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """创建新的API配置"""
    config_dict = config.model_dump()
    config_dict["id"] = datetime.now().strftime("%Y%m%d%H%M%S")
    config_dict["created_at"] = datetime.now().isoformat()

    # 如果未指定厂商标识符，使用base_url的域名作为厂商标识符
    if not config_dict.get("vendor"):
        from urllib.parse import urlparse
        parsed_url = urlparse(config_dict["base_url"])
        config_dict["vendor"] = parsed_url.netloc

    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
        configs.append(config_dict)
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"].append(config_dict)

    return {"message": "配置已创建", "config_id": config_dict["id"]}


@app.put("/api/configs/{config_id}")
async def update_config(config_id: str, config: APIConfig, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """更新现有的API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]

    # 查找要更新的配置
    found = False
    for i, existing_config in enumerate(configs):
        if existing_config["id"] == config_id:
            # 保留原有的id和created_at
            config_dict = config.model_dump()
            config_dict["id"] = config_id
            config_dict["created_at"] = existing_config.get("created_at", datetime.now().isoformat())

            # 如果未指定厂商标识符，保留原有的或使用base_url的域名
            if not config_dict.get("vendor"):
                config_dict["vendor"] = existing_config.get("vendor")
                if not config_dict["vendor"]:
                    from urllib.parse import urlparse
                    parsed_url = urlparse(config_dict["base_url"])
                    config_dict["vendor"] = parsed_url.netloc

            configs[i] = config_dict
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"未找到ID为{config_id}的配置")

    # 保存更新后的配置
    if redis_client:
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"] = configs

    return {"message": "配置已更新", "config_id": config_id}


@app.get("/api/configs")
async def list_configs(api_key: str = Depends(get_admin_api_key_from_cookie)):
    """列出所有API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]

    # 创建配置的深拷贝，避免修改原始数据
    configs_for_display = copy.deepcopy(configs)

    # 隐藏API密钥（仅在显示时）
    for config in configs_for_display:
        config["api_key"] = "**" + config["api_key"][-4:] if len(config["api_key"]) > 4 else "****"

    return {"configs": configs_for_display}


@app.get("/api/configs/{config_id}")
async def get_config(config_id: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """获取单个API配置的详细信息"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]

    # 查找指定的配置
    for config in configs:
        if config["id"] == config_id:
            # 创建配置的深拷贝，避免修改原始数据
            config_copy = copy.deepcopy(config)
            # 隐藏API密钥（仅在显示时）
            config_copy["api_key"] = "**" + config_copy["api_key"][-4:] if len(config_copy["api_key"]) > 4 else "****"
            return config_copy

    raise HTTPException(status_code=404, detail=f"未找到ID为{config_id}的配置")


@app.delete("/api/configs/{config_id}")
async def delete_config(config_id: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """删除指定的API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
        configs = [c for c in configs if c["id"] != config_id]
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"] = [c for c in in_memory_db["api_configs"] if c["id"] != config_id]

    return {"message": "配置已删除"}


# 模型映射相关端点
@app.post("/api/model-mappings")
async def create_model_mapping(mapping: ModelMappingRequest, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """创建或更新模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
        mappings[mapping.unified_name] = mapping.vendor_models
        redis_client.set("model_mappings", json.dumps(mappings))
    else:
        if "model_mappings" not in in_memory_db:
            in_memory_db["model_mappings"] = {}
        in_memory_db["model_mappings"][mapping.unified_name] = mapping.vendor_models

    return {"message": f"模型映射已创建: {mapping.unified_name}"}


@app.get("/api/model-mappings")
async def list_model_mappings(api_key: str = Depends(get_admin_api_key_from_cookie)):
    """列出所有模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
    else:
        mappings = in_memory_db.get("model_mappings", {})

    return {"mappings": mappings}


@app.delete("/api/model-mappings/{unified_name}")
async def delete_model_mapping(unified_name: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """删除指定的模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
        if unified_name in mappings:
            del mappings[unified_name]
            redis_client.set("model_mappings", json.dumps(mappings))
    else:
        if "model_mappings" in in_memory_db and unified_name in in_memory_db["model_mappings"]:
            del in_memory_db["model_mappings"][unified_name]

    return {"message": f"模型映射已删除: {unified_name}"}


def get_config_model_pairs(model: str):
    """
    查找有当前模型的配置，找不到的话，抛异常
    :param model:
    :return: 返回所有符合条件的配置和模型对
    """
    logger.info(f"查找模型配置: {model}")

    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]
        mappings = in_memory_db.get("model_mappings", {})

    logger.info(f"加载了 {len(configs)} 个配置")

    matching_configs = []

    # 遍历所有配置
    for config in configs:
        # 检查配置中的模型映射
        if config.get("model_mappings") and model in config["model_mappings"]:
            actual_model = config["model_mappings"][model]
            # 确保配置支持实际模型
            if actual_model in config["models"]:
                matching_configs.append((config, actual_model))
                logger.info(f"找到配置内映射: {model} -> {actual_model} (配置ID: {config.get('id', UNKNOWN)})")
        
        # 检查是否原生支持该模型
        if model in config["models"]:
            matching_configs.append((config, model))
            logger.info(f"找到原生支持: {model} (配置ID: {config.get('id', UNKNOWN)})")

    if not matching_configs:
        logger.warning(f"没有找到支持模型 {model} 的配置")
        raise HTTPException(status_code=404, detail=f"没有找到支持模型 {model} 的配置")

    return matching_configs



# 获取所有可用模型列表的端点
@app.api_route("/v1/models", methods=["GET", "POST"])
async def list_available_models(request: Request):
    """返回所有可用模型的列表，兼容OpenAI API格式"""
    try:
        # 从请求中获取API密钥进行身份验证
        api_key = await get_api_key_from_request(request)

        # 使用现有函数获取所有模型映射
        mappings_response = await list_model_mappings(api_key=None)  # 直接调用函数，不通过API
        model_mappings = mappings_response.get("mappings", {})

        # 使用现有函数获取所有配置
        configs_response = await list_configs(api_key=None)  # 直接调用函数，不通过API
        all_configs = configs_response.get("configs", [])

        # 提取所有已配置的模型
        all_models = set()

        # 创建一个反向映射字典，用于查找模型别名
        reverse_model_mappings = {}

        # 处理模型映射
        for unified_name, vendor_models in model_mappings.items():
            # 统一模型名称作为别名
            all_models.add(unified_name)

            # 记录实际模型名称到别名的映射
            for vendor_model in vendor_models.values():
                if vendor_model not in reverse_model_mappings:
                    reverse_model_mappings[vendor_model] = unified_name

        # 从配置中获取模型
        for config in all_configs:
            for model in config["models"]:
                # 如果模型有映射别名，使用别名
                if model in reverse_model_mappings:
                    all_models.add(reverse_model_mappings[model])
                else:
                    # 查看配置本身的模型映射
                    model_mappings_in_config = config.get("model_mappings", {})
                    if model_mappings_in_config:
                        # 检查这个模型是否在配置的映射中作为实际模型
                        is_mapped = False
                        for alias, actual_model in model_mappings_in_config.items():
                            if actual_model == model:
                                all_models.add(alias)
                                is_mapped = True

                        # 如果模型没有被映射，则添加原始名称
                        if not is_mapped:
                            all_models.add(model)
                    else:
                        # 没有映射，直接添加原始模型名称
                        all_models.add(model)

        # 按照OpenAI API格式构造响应
        model_list = []
        current_time = int(datetime.now().timestamp() * 1000)  # 毫秒级时间戳

        for model_id in sorted(all_models):
            model_list.append({
                "id": model_id,
                "object": "model",
                "created": current_time,
                "owned_by": "uniapi"
            })

        # 返回最终结果
        return {
            "object": "list",
            "data": model_list
        }

    except HTTPException as e:
        # 重新抛出HTTP异常
        raise e
    except Exception as e:
        # 记录详细错误并返回500错误
        logger.error(f"获取模型列表时出错: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取模型列表时出错: {str(e)}"
        )


# OpenAI兼容端点 - 仅代理chat/completions
async def record_model_request(model_key, request_record, history_records):
    """
    记录模型请求数据，并将其与历史记录合并保存到Redis或内存中
    
    该函数维护了每个模型的请求历史记录，以便于后续基于性能指标进行模型选择。
    
    Args:
        model_key (str): 模型key，用作Redis键
        request_record (ModelRequestRecord): 当前请求的记录对象，包含请求ID、时间、成功状态等
        history_records (Deque[ModelRequestRecord]): 历史请求记录列表，如果为None则创建新列表
    
    Returns:
        Deque[ModelRequestRecord]: 更新后的历史记录列表
    
    记录会保留最多50条记录，且只保留72小时内的记录。
    这些数据用于后续模型选择时进行权重偏移计算，成功率高、响应快的模型会获得更高权重。
    """
    try:
        current_time = int(time.time() * 1000)  # 当前时间（毫秒）
        max_age = 72 * 60 * 60 * 1000  # 72小时（毫秒）
        max_records = 50  # 最大记录数

        # 如果历史记录为空，则初始化为空列表
        if history_records is None:
            history_records = deque()

        # 添加当前记录到历史记录
        if request_record:
            history_records.appendleft(request_record)

        # 过滤掉超过72小时的记录
        filtered_records = deque(record for record in history_records
                                 if (current_time - record.request_time) <= max_age)

        while len(filtered_records) > max_records:
            filtered_records.pop()

        # 尝试使用Redis保存（如果有配置）
        try:
            if redis_client:
                # 将记录转换为JSON并保存到Redis
                serialized_records = json.dumps([record.dict() for record in filtered_records])
                redis_client.set(model_key, serialized_records, ex=int(max_age / 1000))
                logger.debug(f"模型请求记录已保存到Redis: {model_key}")
            else:
                # 如果没有Redis，则保存到内存中
                model_request_history[model_key] = filtered_records
                logger.debug(f"模型请求记录已保存到内存: {model_key}")
        except Exception as e:
            logger.warning(f"保存模型请求记录时出错: {str(e)}")
            # 出错时保存到内存
            model_request_history[model_key] = filtered_records

        return filtered_records
    except Exception as e:
        logger.error(f"记录模型请求时出错: {str(e)}", exc_info=True)
        # 出错时返回原始历史记录
        return history_records


@app.api_route("/v1/chat/completions", methods=["GET", "POST", "PUT", "DELETE"])
async def openai_proxy(request: Request):
    """OpenAI API兼容代理 - 仅支持chat/completions端点"""
    # 验证API密钥（从请求头中获取）
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )

    try:
        # 提取API密钥
        scheme, credentials = auth_header.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
            )
        api_key = credentials

        # 检查API密钥有效性
        if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
            raise HTTPException(
                status_code=401,
                detail="未配置允许的API密钥。请在环境变量中设置TEMP_API_KEY或ADMIN_API_KEY"
            )

        # 验证API密钥
        if api_key != ADMIN_API_KEY and api_key not in ALLOWED_API_KEYS:
            raise HTTPException(
                status_code=401,
                detail="无效的API密钥"
            )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
        )

    # 获取请求内容
    body = await request.body()
    body_dict = json.loads(body) if body else {}

    # 获取模型名称
    model = body_dict.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail="请求中未指定模型")

    logger.info(f"收到API请求: 路径=/v1/chat/completions, 模型={model}")

    try:
        # 首先过滤出包含了当前模型的配置列表
        config_model_pairs = get_config_model_pairs(model)
        config_model_key_list = []
        for config, actual_model in config_model_pairs:
            config_model_key_list.append(build_model_request_record_key(config.get("id", UNKNOWN), actual_model))

        # 查询这些模型的请求历史
        model_request_map = batch_get_model_request_record(config_model_key_list)

        config, actual_model = weighted_choice(config_model_pairs, model_request_map)

        logger.info(f"选择配置: ID={config.get('id', 'unknown')}, 实际模型={actual_model}")

        # 如果实际模型名称与请求的不同，替换请求中的模型名称
        if actual_model != model:
            logger.info(f"模型映射: {model} -> {actual_model}")
            body_dict["model"] = actual_model
            body = json.dumps(body_dict).encode()

        model_request_key = build_model_request_record_key(config.get("id", UNKNOWN), actual_model)
        current_request_history = model_request_map.get(model_request_key)

        # 准备转发
        headers = dict(request.headers)
        headers.pop("host", None)

        # 移除所有形式的authorization头（不区分大小写）
        auth_headers = [k for k in headers.keys() if k.lower() == "authorization"]
        for key in auth_headers:
            headers.pop(key)

        # 更新Content-Length头以匹配新的请求体长度
        content_length_headers = [k for k in headers.keys() if k.lower() == "content-length"]
        for key in content_length_headers:
            headers.pop(key)

        # 添加新的Content-Length头
        if body:
            headers["Content-Length"] = str(len(body))

        # 添加正确的Authorization头
        headers["Authorization"] = f"Bearer {config['api_key']}"

        # 设置正确的base_url和请求URL
        base_url = config["base_url"]
        if base_url.endswith("#"):
            # 如果以#结尾，移除#后直接使用
            url = base_url[:-1]
        elif base_url.endswith("/"):
            # 如果以/结尾，直接拼接chat/completions
            url = f"{base_url}chat/completions"
        else:
            # 默认拼接完整路径
            url = f"{base_url}/v1/chat/completions"

        logger.info(f"转发请求到: {url}")

        # 判断是否为流式请求
        is_stream = body_dict.get("stream", False)

        if is_stream:
            from api.stream_handler import StreamHandler
            handler = StreamHandler(
                request=request,
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=TIMEOUT_SECONDS,
                model_request_key=model_request_key,
                current_request_history=current_request_history
            )
            return handler.get_response()
        else:
            # 非流式请求的处理
            async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_SECONDS) as client:
                response = await client.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body
                )

                # 直接返回响应内容
                return JSONResponse(
                    content=response.json(),
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
    except Exception as e:
        # 添加详细的错误日志
        logger.error(f"处理请求时出错: {str(e)}")
        # 返回友好的错误信息
        return JSONResponse(
            content={"error": "处理请求时出错", "message": str(e)},
            status_code=500
        )


# 页面路由

# 主页 - 重定向到登录页
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


# 登录页面
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin", response_class=HTMLResponse)
async def admin_login(request: Request, api_key: str = Form(...), remember_me: Optional[bool] = Form(False)):
    # 验证API密钥
    if api_key == ADMIN_API_KEY:
        # 设置cookie而不是返回JSON
        response = templates.TemplateResponse("admin.html", {"request": request})
        response.set_cookie(key="auth_key", value=api_key, httponly=True)
        # 如果选择记住密钥，设置更长的过期时间
        if remember_me:
            response.set_cookie(key="remember_auth", value="true", max_age=30 * 24 * 60 * 60, httponly=True)
        return response
    else:
        # 验证失败，返回错误信息
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "无效的API密钥或无权访问管理面板"
        })


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # 从cookie中获取API密钥
    auth_key = request.cookies.get("auth_key")
    if not auth_key or auth_key != ADMIN_API_KEY:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("admin.html", {"request": request})


# 健康检查端点
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


# 获取用于调试的API密钥
async def get_api_key_from_request(request: Request):
    """从请求头中提取API密钥"""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )

    try:
        # 提取API密钥
        scheme, credentials = auth_header.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
            )
        api_key = credentials

        # 检查API密钥有效性
        if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
            raise HTTPException(
                status_code=401,
                detail="未配置允许的API密钥。请在环境变量中设置TEMP_API_KEY或ADMIN_API_KEY"
            )

        # 验证API密钥
        if api_key != ADMIN_API_KEY and api_key not in ALLOWED_API_KEYS:
            raise HTTPException(
                status_code=401,
                detail="无效的API密钥"
            )

        return api_key
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
        )


def build_model_request_record_key(config_id, model_name):
    """
    构建模型的唯一key，每个配置下的模型视为唯一
    :param config_id: 配置id
    :param model_name: 模型名
    :return:
    """
    model_key = build_model_key(config_id, model_name)
    return f"request_r_{model_key}"


def build_model_key(config_id, model_name):
    """
    构建模型的唯一key，每个配置下的模型视为唯一
    :param config_id: 配置id
    :param model_name: 模型名
    :return:
    """
    key = f"{config_id}-{model_name}"
    return hashlib.md5(key.encode()).hexdigest()


def batch_get_model_request_record(model_key_list):
    """
    批量获取模型的请求记录列表
    :param model_key_list:
    :return:
    """
    try:
        # 构建key列表
        final_key_list = model_key_list

        result = {}

        if redis_client:
            # 由于这是在同步函数中，我们不能直接使用await
            records_json_list = []
            try:
                # 对于同步Redis客户端
                records_json_list = redis_client.mget(final_key_list)
            except Exception as e:
                # 如果Redis客户端是异步的，则需要在外部处理
                logger.warning(f"从redis获取模型请求历史记录时出错: {str(e)}")
                pass

            if records_json_list and len(records_json_list) == len(final_key_list):
                # 反序列化记录
                for i in range(len(final_key_list)):
                    model_key = final_key_list[i]
                    records_json = records_json_list[i]
                    if not records_json:
                        continue
                    records_data = json.loads(records_json)
                    history_records = deque(ModelRequestRecord(**record) for record in records_data)
                    result[model_key] = history_records

        else:
            for final_key in final_key_list:
                history_records = model_request_history.get(final_key)
                result[final_key] = history_records

        return result
    except Exception as e:
        logger.warning(f"获取模型请求历史记录时出错: {str(e)}")
        return {}


def count_recent_consecutive_failures(request_record_history):
    """
    计算最近连续失败的次数，注意，外部需要保证传入的历史是按照从近到远排序的
    :param request_record_history:
    :return:
    """
    failure_count = 0
    for record in request_record_history:
        if not record.request_success:
            failure_count += 1
        else:
            break
    return failure_count


def filter_valid_config_model_pairs(config_model_pairs, model_request_map):
    if not config_model_pairs or len(config_model_pairs) == 0:
        return []

    if not model_request_map or len(model_request_map) == 0:
        return config_model_pairs

    valid_config_model_pairs = []
    for config, actual_model in config_model_pairs:
        key = build_model_request_record_key(config.get("id", UNKNOWN), actual_model)
        _model_request_history = model_request_map.get(key)
        if _model_request_history:
            # 断路器机制，记录失败次数，如果连续失败次数达到阈值，就进行降级处理
            recent_fail_count = count_recent_consecutive_failures(_model_request_history)
            if recent_fail_count > 2:
                # 理论上不会拿到兜底的值，因为只存了最近72小时的记录，按照断路时间算是拿不到的
                cooldown_seconds = fail_count_to_cooldown.get(recent_fail_count, 24 * 60 * 60)
                if time.time() - _model_request_history[0].request_time / 1000 > cooldown_seconds:
                    # 如果已经冷却了，就直接加进去
                    valid_config_model_pairs.append((config, actual_model))
                else:
                    # 如果没有冷却，就忽略
                    pass
            else:
                valid_config_model_pairs.append((config, actual_model))
        else:
            # 没有调用历史，可以直接加进去
            valid_config_model_pairs.append((config, actual_model))

    # 如果全都过滤完了，就都加回来重选
    if not valid_config_model_pairs or len(valid_config_model_pairs) == 0:
        valid_config_model_pairs = config_model_pairs

    return valid_config_model_pairs


# 基于历史请求数据的权重选择算法
def weighted_choice(config_model_pairs, model_request_map):
    """
    基于历史请求数据的权重选择算法，用于智能选择最优的API配置和模型
    
    该算法考虑了历史请求的成功率和首字符响应时间，为性能更好的模型赋予更高的选择权重。
    具体权重计算方式：
    1. 基础权重为1.0
    2. 如果有历史数据，则：
       - 计算成功率权重 = 成功请求数 / 总请求数
       - 对于成功的请求，计算响应时间权重 = 基准时间(2000ms) / 平均首字符响应时间
       - 综合权重 = 成功率权重 * 响应时间权重
    3. 确保最小权重为0.1，避免模型被完全排除
    4. 归一化所有权重并进行加权随机选择
    
    Args:
        config_model_pairs (List[Tuple[Dict, str]]): 配置和模型名称的元组列表 [(config, model_name), ...]
        model_request_map (Dict[str, List[ModelRequestRecord]): 模型的最近请求记录
    
    Returns:
        Tuple[Dict, str]: 选择的配置和模型名称元组 (config, model_name)
    """
    if not config_model_pairs:
        return None

    valid_config_model_pairs = filter_valid_config_model_pairs(config_model_pairs, model_request_map)

    # 如果只有一个选项，直接返回
    if len(valid_config_model_pairs) == 1:
        return valid_config_model_pairs[0]

    # 为每个配置计算权重
    weights = []

    for config, model_name in valid_config_model_pairs:
        # 默认权重为1.0
        weight = 1.0
        key = build_model_request_record_key(config.get("id", UNKNOWN), model_name)

        # 尝试获取历史请求数据
        history_records = model_request_map.get(key)

        if history_records and len(history_records) > 0:
            # 计算成功率
            success_count = sum(1 for r in history_records if r.request_success)
            success_rate = success_count / len(history_records)

            # 计算平均首字符响应时间（仅考虑成功地请求）
            successful_requests = [r for r in history_records if r.request_success and r.first_token_rt > 0]

            if not successful_requests:
                # 有请求，但是没有成功，立刻降低权重，第一次降为0.2，然后递减
                weight = 0.2 / len(history_records)
            else:
                avg_first_token_time = sum(r.first_token_rt for r in successful_requests) / len(successful_requests)
                response_time_factor = 200 / max(avg_first_token_time, 100)
                # 引入非线性变换强化成功率影响（示例：平方）
                success_factor = success_rate ** 2
                weight = response_time_factor * success_factor
                logger.debug(f"模型 {model_name} 动态权重: 成功率={success_rate:.2f} 响应={avg_first_token_time:.0f}ms 权重={weight:.4f}")

        weights.append(weight)

    # 归一化权重
    total_weight = sum(weights)
    normalized_weights = [w / total_weight for w in weights]

    # 根据权重进行随机选择
    return random.choices(valid_config_model_pairs, weights=normalized_weights, k=1)[0]
