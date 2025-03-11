import json
import logging
import time
import uuid
import asyncio
from datetime import datetime

import httpx
from fastapi.responses import StreamingResponse
from api.models import ModelRequestRecord
from api.index import record_model_request

logger = logging.getLogger("uniapi")


class StreamHandler:
    def __init__(self, request, url, headers, body, timeout_seconds, model_request_key=None,
                 current_request_history=None):
        self.request = request
        self.url = url
        self.headers = headers
        self.body = body
        self.timeout_seconds = timeout_seconds
        self.model_request_key = model_request_key
        self.current_request_history = current_request_history

        # 使用 asyncio.Queue 替代 deque
        self.response_queue = asyncio.Queue()
        # 标记上游响应是否已经全部接收完毕
        self.upstream_complete = False
        # 记录总字符数和消费时间，用于计算理想流速
        self.total_chars = 0
        self.consumption_start_time = None
        # 请求开始时间和首个token标记
        self.request_start_time = int(time.time() * 1000)
        self.first_token = False
        # 请求记录
        self.request_record = None
        # 添加锁以保护字符输出
        self.output_lock = asyncio.Lock()
        # 当前正在处理的字段
        self.current_field = None
        # 当前消息的ID和模型
        self.current_message_id = None
        self.current_model = None

    @staticmethod
    def extract_content_from_chunk(chunk_data):
        """解析JSON并提取content和reasoning_content字段"""
        try:
            # 检查输入是否为空
            if not chunk_data:
                return {'raw_data': '', 'content': '', 'reasoning_content': '', 'is_done': False}

            # 移除SSE前缀 "data: "
            if isinstance(chunk_data, bytes):
                if chunk_data.startswith(b'data: '):
                    json_str = chunk_data[6:].decode('utf-8').strip()
                else:
                    json_str = chunk_data.decode('utf-8').strip()
            else:
                json_str = str(chunk_data).strip()

            # 处理SSE结束标记
            if json_str == '[DONE]':
                return {'raw_data': '[DONE]', 'content': '', 'reasoning_content': '', 'is_done': True}

            # 解析JSON
            data = json.loads(json_str)

            # 提取content和reasoning_content
            content = ""
            reasoning_content = ""

            if 'choices' in data and len(data.get('choices', [])) > 0:
                choice = data['choices'][0]
                if 'delta' in choice:
                    delta = choice.get('delta', {})
                    content = delta.get('content', '')
                    reasoning_content = delta.get('reasoning_content', '')
                elif 'message' in choice:
                    message = choice.get('message', {})
                    content = message.get('content', '')
                    reasoning_content = message.get('reasoning_content', '')

            # 返回原始数据和提取的内容
            return {
                'raw_data': data,
                'content': content or '',  # 确保返回空字符串而不是None
                'reasoning_content': reasoning_content or '',  # 确保返回空字符串而不是None
                'is_done': False
            }
        except Exception as e:
            logger.error(f"解析响应块出错: {str(e)}, 原始数据: {chunk_data}")
            # 返回一个有效地响应，避免后续处理出错
            return {'raw_data': '', 'content': '', 'reasoning_content': '', 'is_done': False}

    async def consume_upstream(self, client):
        """消费上游响应的协程"""
        try:
            async with client.stream(
                    self.request.method,
                    self.url,
                    headers=self.headers,
                    content=self.body
            ) as response:
                # 检查状态码
                if not response.is_success:
                    error_msg = f"data: {{\"error\":\"上游服务器返回错误: {response.status_code}\"}}\n\n".encode()
                    await self.response_queue.put(error_msg)
                    return

                # 开始记录消费时间
                self.consumption_start_time = datetime.now()

                # 直接逐块读取和处理内容
                async for chunk in response.aiter_bytes():
                    if not chunk:  # 跳过空块
                        continue

                    if not self.first_token:
                        first_token_time = int(datetime.now().timestamp() * 1000)
                        self.first_token = True
                        self.request_record.first_token_rt = first_token_time - self.request_start_time

                    # 解析并提取内容
                    parsed_chunk = self.extract_content_from_chunk(chunk)
                    if parsed_chunk.get('is_done', False):
                        # 如果是[DONE]标记，发送一个完成消息
                        done_msg = "data: [DONE]\n\n".encode('utf-8')
                        await self.response_queue.put(done_msg)
                    else:
                        # 更新总字符数（确保使用空字符串的长度）
                        content = parsed_chunk.get('content', '') or ''
                        reasoning_content = parsed_chunk.get('reasoning_content', '') or ''
                        self.total_chars += len(content)
                        self.total_chars += len(reasoning_content)
                        # 将解析后的内容添加到队列
                        if content or reasoning_content:  # 只有当有内容时才添加到队列
                            await self.response_queue.put(parsed_chunk)

        except Exception as e:
            logger.error(f"消费上游响应时出错: {str(e)}", exc_info=True)
            error_msg = f"data: {{\"error\":\"消费上游响应时出错: {str(e)}\"}}\n\n".encode()
            await self.response_queue.put(error_msg)
        finally:
            self.upstream_complete = True

    async def process_chunk(self, chunk_data):
        """处理单个数据块，确保线程安全"""
        if isinstance(chunk_data, bytes):
            return chunk_data

        async with self.output_lock:
            responses = []

            for field in ['content', 'reasoning_content']:
                text = chunk_data.get(field, '')
                if not text:
                    continue

                # 如果是新的字段或新的消息，更新当前状态
                if (self.current_field != field or
                        self.current_message_id != chunk_data.get('raw_data', {}).get('id') or
                        self.current_model != chunk_data.get('raw_data', {}).get('model')):
                    self.current_field = field
                    self.current_message_id = chunk_data.get('raw_data', {}).get('id')
                    self.current_model = chunk_data.get('raw_data', {}).get('model')

                # 简单地将文本分成小块
                for i in range(0, len(text), 3):
                    small_chunk = text[i:i + 3]
                    if not small_chunk:
                        continue

                    # 创建一个完整的OpenAI格式响应
                    response = {
                        "id": self.current_message_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.current_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    field: small_chunk
                                }
                            }
                        ]
                    }
                    responses.append(f"data: {json.dumps(response)}\n\n".encode('utf-8'))

            return responses

    async def generate_small_chunks(self):
        """异步生成小块内容"""
        # 初始化变量
        ideal_speed = None  # 初始时不设置速度
        last_output_time = time.time()
        min_chars_for_speed = 20  # 至少收集20个字符后再计算速度
        done_sent = False

        try:
            while True:
                # 计算剩余的超时时间
                elapsed_time = (datetime.now() - datetime.fromtimestamp(self.request_start_time/1000)).total_seconds()
                remaining_time = self.timeout_seconds - elapsed_time - 5  # 留5秒缓冲

                # 如果剩余时间极少或上游完成，不做延迟直接输出
                no_delay = remaining_time < 3 or self.upstream_complete

                # 尝试从队列获取数据，设置超时时间
                try:
                    chunk_data = await asyncio.wait_for(
                        self.response_queue.get(),
                        timeout=0.1 if not self.upstream_complete else None
                    )
                except asyncio.TimeoutError:
                    if self.upstream_complete and self.response_queue.empty() and not done_sent:
                        # 发送finish_reason消息
                        if self.current_message_id and self.current_model:
                            finish_msg = {
                                "id": self.current_message_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": self.current_model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(finish_msg)}\n\n".encode('utf-8')
                        yield "data: [DONE]\n\n".encode('utf-8')
                        done_sent = True
                        break
                    continue

                if not chunk_data:
                    continue

                # 处理数据块
                processed_chunks = await self.process_chunk(chunk_data)
                if not processed_chunks:
                    continue

                # 如果是错误消息或[DONE]标记，直接输出
                if isinstance(processed_chunks, bytes):
                    if chunk_data == b'data: [DONE]\n\n' and not done_sent:
                        # 在[DONE]之前发送finish_reason消息
                        if self.current_message_id and self.current_model:
                            finish_msg = {
                                "id": self.current_message_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": self.current_model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(finish_msg)}\n\n".encode('utf-8')
                        yield chunk_data
                        done_sent = True
                        break
                    elif not chunk_data.startswith(b'data: [DONE]'):  # 只输出非[DONE]的错误消息
                        yield processed_chunks
                        break
                    continue

                # 默认情况下，按照15个字符每秒
                ideal_speed = ideal_speed or 15

                # 计算输出速度（如果收集了足够的样本）
                if self.total_chars >= min_chars_for_speed:
                    elapsed_seconds = (datetime.now() - self.consumption_start_time).total_seconds()
                    if elapsed_seconds > 0:
                        _ideal_speed = self.total_chars / elapsed_seconds
                        # 确保速度在合理范围内
                        _ideal_speed = max(min(_ideal_speed, 100), 5)  # 每秒5-200个字符
                        ideal_speed = _ideal_speed * 0.4 + ideal_speed * 0.6
                        logger.debug(f"计算得到的理想速度: {ideal_speed} 字符/秒")

                # 如果剩余时间不足，加速输出
                if remaining_time < 10:
                    ideal_speed = ideal_speed * 2

                current_time = time.time()
                time_since_last = current_time - last_output_time

                # 输出所有块
                for chunk in processed_chunks:
                    # 只在非无延迟模式下控制速度
                    if not no_delay:
                        # 计算应该等待的时间
                        target_interval = 1.0 / ideal_speed  # 每个字符的理想间隔
                        if time_since_last < target_interval:
                            await asyncio.sleep(target_interval - time_since_last)
                    
                    yield chunk
                    time_since_last = 0
                    last_output_time = time.time()

        except Exception as e:
            logger.error(f"生成小块时出错: {str(e)}", exc_info=True)
            error_msg = f"data: {{\"error\":\"生成小块时出错: {str(e)}\"}}\n\n".encode()
            yield error_msg
            # 发送finish_reason消息（错误情况）
            if not done_sent:
                if self.current_message_id and self.current_model:
                    finish_msg = {
                        "id": self.current_message_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.current_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    }
                    yield f"data: {json.dumps(finish_msg)}\n\n".encode('utf-8')
                yield "data: [DONE]\n\n".encode('utf-8')

    async def process_stream(self):
        """处理流式响应"""
        client = httpx.AsyncClient(follow_redirects=True, timeout=self.timeout_seconds)

        # ModelRequestRecord构建
        self.request_record = ModelRequestRecord(
            request_time=self.request_start_time,
            request_id=str(uuid.uuid4()),
            first_token_rt=-1,
            request_success=False,
            is_streaming=True,
            request_type="chat",
        )

        try:
            # 创建消费者任务
            consumer_task = asyncio.create_task(self.consume_upstream(client))

            # 直接使用异步生成器
            async for chunk in self.generate_small_chunks():
                yield chunk

            # 确保消费者任务完成
            await consumer_task

        except Exception as e:
            logger.error(f"流式处理出错: {str(e)}", exc_info=True)
            error_msg = f"data: {{\"error\":\"流式处理出错: {str(e)}\"}}\n\n".encode()
            yield error_msg

        finally:
            self.request_record.request_success = True if self.first_token else False
            if not self.first_token:
                self.request_record.first_token_rt = -1

            if self.model_request_key:
                await record_model_request(self.model_request_key, self.request_record, self.current_request_history)
            await client.aclose()

    def get_response(self):
        """获取StreamingResponse对象"""
        return StreamingResponse(
            self.process_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "text/event-stream"
            }
        )
