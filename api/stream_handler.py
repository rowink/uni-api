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

CHUNK_SPLITTER = b"\n\n"

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

        # 是否输出finish reason
        self.finish_reason_sent = False

    @staticmethod
    def extract_content_from_chunk(msg):
        """解析JSON并提取content和reasoning_content字段"""
        try:
            # 非openAi数据消息，可能为其他心跳包或控制消息，直接返回
            if not msg.startswith(b'data: '):
                return {'raw_data': msg + CHUNK_SPLITTER}

            # 移除SSE前缀 "data: "
            data = msg[6:]
            if not data or data.isspace():
                return {'raw_data': msg + CHUNK_SPLITTER}

            if data == b'[DONE]':
                return {'raw_data': msg + CHUNK_SPLITTER}


            try:
                json_data = json.loads(data)
            except json.JSONDecodeError:
                return {'raw_data': data + CHUNK_SPLITTER}

            if not json_data:
                return {'raw_data': data + CHUNK_SPLITTER}

            # 提取content和reasoning_content
            content = ""
            reasoning_content = ""
            finish_reason = None

            if 'choices' in json_data and len(json_data.get('choices', [])) > 0:
                choice = json_data['choices'][0]
                if 'delta' in choice:
                    delta = choice.get('delta', {})
                    content = delta.get('content', '')
                    reasoning_content = delta.get('reasoning_content', '')
                if 'finish_reason' in choice:
                    finish_reason = choice.get('finish_reason', None)

            # 返回原始数据和提取的内容
            return {
                'raw_data': data + CHUNK_SPLITTER,
                'content': content or '',  # 确保返回空字符串而不是None
                'reasoning_content': reasoning_content or '',  # 确保返回空字符串而不是None
                'json_data': json_data,  # 解析好的原始json数据
                'need_process': True,  # 标记需要处理
                'finish_reason': finish_reason
            }
        except Exception as e:
            logger.error(f"解析响应块出错: {str(e)}, 原始数据: {msg}")
            # 返回None，后面过滤
            return None

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

                buffer = b""

                # 直接逐块读取和处理内容
                async for chunk in response.aiter_bytes():
                    if not chunk:  # 跳过空块
                        continue

                    if not self.first_token:
                        first_token_time = int(datetime.now().timestamp() * 1000)
                        self.first_token = True
                        self.request_record.first_token_rt = first_token_time - self.request_start_time

                    buffer += chunk

                    # 解析并提取内容
                    while CHUNK_SPLITTER in buffer:
                        msg, buffer = buffer.split(CHUNK_SPLITTER, 1)
                        parsed_chunk = self.extract_content_from_chunk(msg)
                        if not parsed_chunk:
                            continue

                        # 更新总字符数（确保使用空字符串的长度）
                        content = parsed_chunk.get('content', '') or ''
                        reasoning_content = parsed_chunk.get('reasoning_content', '') or ''
                        self.total_chars += len(content)
                        self.total_chars += len(reasoning_content)
                        # 不为空就要添加到队列，这样才能正常结束
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

            # {
            #     'raw_data': data + CHUNK_SPLITTER,
            #     'content': content or '',  # 确保返回空字符串而不是None
            #     'reasoning_content': reasoning_content or '',  # 确保返回空字符串而不是None
            #     'json_data': json_data,  # 解析好的原始json数据
            #     'need_process': True,  # 标记需要处理
            # }

            need_process = chunk_data.get('need_process', False)
            if not need_process:
                return chunk_data['raw_data']

            responses = []

            json_data = chunk_data.get('json_data', {})

            msg_id = json_data.get('id', '')
            msg_model = json_data.get('model', '')
            msg_obj = json_data.get('object', '')
            msg_created = json_data.get('created', 0)
            msg_finish_reason = json_data.get('finish_reason', None)

            if not self.current_message_id:
                self.current_message_id = msg_id
            if not self.current_model:
                self.current_model = msg_model

            for field in ['content', 'reasoning_content']:
                text = chunk_data.get(field, '')
                if not text:
                    continue

                # 简单地将文本分成小块
                for i in range(0, len(text), 3):
                    small_chunk = text[i:i + 3]
                    if not small_chunk:
                        continue

                    # 创建一个完整的OpenAI格式响应
                    response = {
                        "id": msg_id,
                        "object": msg_obj,
                        "created": msg_created,
                        "model": msg_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    field: small_chunk
                                }
                            }
                        ]
                    }
                    # 仅在循环的最后一次添加上游可能存在的finish_reason
                    if i == len(text) - 3 or len(small_chunk) < 3:
                        if msg_finish_reason:
                            self.finish_reason_sent = True
                        response["choices"][0]["finish_reason"] = msg_finish_reason

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
                elapsed_time = (datetime.now() - datetime.fromtimestamp(self.request_start_time / 1000)).total_seconds()
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
                    continue

                if not chunk_data:
                    continue

                # 处理数据块
                processed_chunks = await self.process_chunk(chunk_data)
                if not processed_chunks:
                    continue

                # 如果非数组，直接输出
                if isinstance(processed_chunks, bytes):
                    if b'[DONE]' in processed_chunks:
                        if not self.finish_reason_sent:
                            self.finish_reason_sent = True
                            finish_msg = {
                                "id": self.current_message_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": self.current_model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(finish_msg)}\n\n".encode('utf-8')
                        yield processed_chunks
                        break
                    yield processed_chunks
                    continue

                # 默认情况下，按照20个字符每秒
                ideal_speed = ideal_speed or 20

                # 计算输出速度（如果收集了足够的样本）
                if self.total_chars >= min_chars_for_speed:
                    elapsed_seconds = (datetime.now() - self.consumption_start_time).total_seconds()
                    if elapsed_seconds > 0:
                        _ideal_speed = self.total_chars / elapsed_seconds
                        # 确保速度在合理范围内
                        _ideal_speed = max(min(_ideal_speed, 100), 5)  # 每秒5-200个字符
                        ideal_speed = _ideal_speed * 0.7 + ideal_speed * 0.3
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
                        # 计算应该等待的时间，因为每个chunk理想情况为3个字符，所以这里要用3除
                        target_interval = 3.0 / ideal_speed  # 每个字符的理想间隔
                        if time_since_last < target_interval:
                            await asyncio.sleep(target_interval - time_since_last)

                    yield chunk
                    time_since_last = 0
                    last_output_time = time.time()

        except Exception as e:
            logger.error(f"生成小块时出错: {str(e)}", exc_info=True)
            error_msg = f"data: {{\"error\":\"生成小块时出错: {str(e)}\"}}\n\n".encode()
            yield error_msg

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
