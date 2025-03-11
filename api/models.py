from pydantic import BaseModel, Field


class ModelRequestRecord(BaseModel):
    """模型请求记录，用于负载均衡分流，包含请求时间，请求结果，首字符生成时间"""
    request_id: str = Field(..., description="请求ID")
    request_time: int = Field(..., description="请求时间，单位为毫秒")
    request_success: bool = Field(..., description="请求结果，True 表示成功，False 表示失败")
    first_token_rt: float = Field(..., description="首字符生成间隔时间，单位为毫秒,失败填写-1")
    is_streaming: bool = Field(..., description="是否为流式响应，True 表示是，False 表示否")
    request_type: str = Field(..., description="请求类型，如：chat, embedding等等") 