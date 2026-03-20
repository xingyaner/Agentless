import os
import json
import logging
from abc import ABC, abstractmethod
from typing import List
from llama_index.llms.openai_like import OpenAILike


# =================================================================
# --- 1. 基础类定义 (保持 Agentless 架构) ---
# =================================================================

class DecoderBase(ABC):
    def __init__(
            self,
            name: str,
            logger,
            batch_size: int = 1,
            temperature: float = 0.0,
            max_new_tokens: int = 8192,
    ) -> None:
        self.name = name
        self.logger = logger
        self.batch_size = batch_size
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens

    @abstractmethod
    def codegen(self, prompt: str, num_samples: int = 1, **kwargs) -> List[dict]:
        pass

    def __repr__(self) -> str:
        return self.name


# =================================================================
# --- 2. 核心适配类：DeepSeek 通用解码器 (支持 V3 与 R1) ---
# =================================================================

class DeepSeekDecoder(DecoderBase):
    """
    【最终稳定版】自动识别 DeepSeek 模型类型。
    如果是 Reasoner (R1)，则动态开启思考模式并捕获思维链。
    如果是 Chat (V3)，则以标准模式运行。
    """

    def __init__(self, api_key, api_base, max_tokens=8192, **kwargs):
        model_name = kwargs.pop("model", "deepseek-chat")
        logger = kwargs.pop("logger", logging.getLogger("model"))
        temp = kwargs.pop("temperature", 0.0)

        super().__init__(model_name, logger, temperature=temp, max_new_tokens=max_tokens)

        # 构建基础参数
        llm_args = {
            "model": model_name,
            "api_key": api_key,
            "api_base": api_base,
            "max_tokens": max_tokens,
            "is_chat_model": True,
            "timeout": 600
        }

        # 核心逻辑：如果是 R1 模型，显式开启思考模式 (thinking: enabled)
        if "reasoner" in model_name.lower():
            llm_args["additional_kwargs"] = {"extra_body": {"thinking": {"type": "enabled"}}}

        self.llm = OpenAILike(**llm_args)

    def codegen(self, prompt: str, num_samples: int = 1, **kwargs) -> List[dict]:
        """
        【Chat 模式加固版】为 V3 增加自动重试，解决长文本传输中断问题。
        """
        import time
        results = []
        max_retries = 3  # 最大重试 3 次

        for i in range(num_samples):
            response = None
            last_error = None

            for attempt in range(max_retries):
                try:
                    # 调用 DeepSeek-Chat 执行预测
                    response = self.llm.complete(prompt)
                    if response:
                        break  # 成功拿到结果，退出重试
                except Exception as e:
                    last_error = e
                    # 计算退避时间：5s, 10s, 15s
                    wait_time = (attempt + 1) * 5
                    self.logger.warning(
                        f"--- [API Connection Warning] Attempt {attempt + 1} failed. Retrying in {wait_time}s... ---")
                    time.sleep(wait_time)

            if not response:
                self.logger.error(f"--- [API Final Failure] {self.name} failed after {max_retries} retries. ---")
                raise last_error

            # 兼容性处理：Chat 模式通常没有 reasoning_content
            reasoning = "N/A (Standard Chat Mode)"
            if hasattr(response, 'raw') and response.raw:
                try:
                    raw_msg = response.raw.choices[0].message
                    if hasattr(raw_msg, 'reasoning_content'):
                        reasoning = raw_msg.reasoning_content
                except:
                    pass

            # 全量归档日志
            output_bundle = (
                f"\n{'=' * 30} DeepSeek {self.name} Sample {i + 1} {'=' * 30}\n"
                f"💡 [REASONING CHAIN]: {reasoning}\n"
                f"📝 [FINAL CONTENT]\n{response.text}\n"
                f"{'=' * 75}\n"
            )
            self.logger.info(output_bundle)
            print(output_bundle)

            results.append({
                "response": response.text,
                "usage": getattr(response.raw, 'usage', {"prompt_tokens": 0, "completion_tokens": 0})
            })
        return results

    def is_direct_completion(self) -> bool:
        return False


# 定义别名，确保老代码引用 DeepSeekR1Wrapper 时不会报错
DeepSeekR1Wrapper = DeepSeekDecoder


# =================================================================
# --- 3. 模型工厂函数 (唯一版本) ---
# =================================================================

def make_model(model, logger, backend, **kwargs):
    """
    统一模型生成入口，根据 model 参数自动配置。
    """
    api_key = os.getenv("DPSEEK_API_KEY")
    api_base = "https://api.deepseek.com"

    # 智能处理 Token 限制
    m_tokens = kwargs.pop('max_tokens', 8192)
    # 如果是 Reasoner 模型，强制开启更大的 Token 窗口以容纳思维链
    if "reasoner" in model.lower():
        m_tokens = max(m_tokens, 16384)

    return DeepSeekDecoder(
        api_key=api_key,
        api_base=api_base,
        max_tokens=m_tokens,
        model=model,
        logger=logger,
        **kwargs
    )


# =================================================================
# --- 4. 辅助占位类 (保持兼容性) ---
# =================================================================

class OpenAIChatDecoder(DecoderBase):
    def codegen(self, *args, **kwargs): return []

    def is_direct_completion(self): return False


class AnthropicChatDecoder(DecoderBase):
    def codegen(self, *args, **kwargs): return []

    def is_direct_completion(self): return False