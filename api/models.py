from pydantic import BaseModel, Field
import time
import threading


class ModelRequestRecord(BaseModel):
    """模型请求记录，用于负载均衡分流，包含请求时间，请求结果，首字符生成时间"""
    request_id: str = Field(..., description="请求ID")
    request_time: int = Field(..., description="请求时间，单位为毫秒")
    request_success: bool = Field(..., description="请求结果，True 表示成功，False 表示失败")
    first_token_rt: float = Field(..., description="首字符生成间隔时间，单位为毫秒,失败填写-1")
    is_streaming: bool = Field(..., description="是否为流式响应，True 表示是，False 表示否")
    request_type: str = Field(..., description="请求类型，如：chat, embedding等等")


class TokenBucket:
    def __init__(self, rate, capacity):
        self.rate = float(rate)  # 漏出速率 (数据包/秒)
        self.capacity = float(capacity)  # 桶的容量
        self.size = 0  # 当前桶中的数据包数量
        self.last_leak = time.time()  # 上次漏出的时间
        self.lock = threading.Lock()  # 保护共享资源

    def consume(self, tokens):  # consume 现在表示尝试添加数据包
        with self.lock:
            if tokens <= 0:
                return True  # 允许 0 令牌的 "空" 操作
            return self._add(tokens)  # 内部函数处理添加

    def _add(self, tokens):
        now = time.time()
        elapsed_time = now - self.last_leak
        self._leak(elapsed_time)  # 先漏掉一些
        if self.size + tokens <= self.capacity:
            self.size += tokens
            return True  # 添加成功
        else:
            return False  # 桶已满，添加失败

    def _leak(self, elapsed_time):
        leaked = elapsed_time * self.rate
        self.size = max(0, self.size - leaked)
        self.last_leak = time.time()

    def update_rate(self, new_rate):
        with self.lock:
            self.rate = float(new_rate)

    def get_current_size(self):
        with self.lock:
            now = time.time()
            elapsed_time = now - self.last_leak
            self._leak(elapsed_time)
            return self.size
